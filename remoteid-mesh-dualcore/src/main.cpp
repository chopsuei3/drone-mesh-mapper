#if !defined(ARDUINO_ARCH_ESP32)
  #error "This program requires an ESP32S3"
#endif

#include <Arduino.h>
#include <HardwareSerial.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_event.h>
#include <esp_system.h>
#include <nvs_flash.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include "dji_droneid.h"
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <atomic>

const int SERIAL1_RX_PIN = 6;
const int SERIAL1_TX_PIN = 5;

// ---------------------------------------------------------------------------
// Wi-Fi channel hopping
//
// Remote ID reaches us two ways over Wi-Fi, and they want opposite things:
//
//   * NAN action frames always ride the NAN social channel, 6.
//   * Beacon-borne Remote ID (and DJI's own DroneID beacons) follow the
//     aircraft's link channel, which can be any of them.
//
// So alternate: channel 6, one other, channel 6, the next other... which keeps
// roughly half the dwell on 6 while still sweeping the band.
//
// 1-11 only: the default Wi-Fi country policy rejects 12-14, so setting them
// would silently fail. Add them here if your regulatory domain allows.
// ---------------------------------------------------------------------------
#define CHANNEL_HOME 6
static const uint8_t channels_24[] = {1, 2, 3, 4, 5, 7, 8, 9, 10, 11};
#define NUM_24_CHANNELS (sizeof(channels_24) / sizeof(channels_24[0]))
#define HOP_DWELL_HOME_MS  150
#define HOP_DWELL_OTHER_MS 100

// One byte, one writer (the hop task), read by the Wi-Fi callback and the
// status line. A volatile byte is atomic on this core, so no lock is needed.
volatile uint8_t current_channel = CHANNEL_HOME;

enum RadioBand : uint8_t { BAND_2_4GHZ, BAND_BLE };
enum IdType    : uint8_t { ID_ODID, ID_DJI };

const char* bandToString(RadioBand band) {
  switch (band) {
    case BAND_2_4GHZ: return "2.4GHz";
    case BAND_BLE:    return "BLE";
    default:          return "unknown";
  }
}

struct id_data {
  uint8_t   mac[6];
  bool      used;             // slot occupied - a MAC may legitimately start 0x00
  int       rssi;
  uint32_t  last_seen;
  char      op_id[ODID_ID_SIZE + 1];
  char      uav_id[ODID_ID_SIZE + 1];
  double    lat_d;
  double    long_d;
  double    base_lat_d;       // operator position (ODID System message)
  double    base_long_d;
  double    home_lat_d;       // takeoff point (DJI DroneID)
  double    home_long_d;
  int       altitude_msl;
  int       height_agl;
  int       speed;
  int       heading;
  RadioBand band;
  uint8_t   channel;          // 0 for BLE
  IdType    id_type;
  uint8_t   product_type;     // DJI model code; 0 when unknown
};

// Which fields an update carries. Remote ID arrives as separate messages
// (BasicID, Location, System...), so an update only overwrites what it has and
// the slot accumulates the rest.
enum : uint8_t { F_ID = 1, F_LOC = 2, F_SYS = 4, F_OP = 8, F_HOME = 16 };

#define MAX_UAVS 8
#define PRINT_QUEUE_LEN 32    // deep enough to ride out a burst while USB drains
static id_data uavs[MAX_UAVS];
static BLEScan* pBLEScan = nullptr;
static ODID_UAS_Data UAS_data;          // Wi-Fi callback only: one task, no lock needed
static QueueHandle_t printQueue = nullptr;
static const char *boot_reason = "unknown";

// uavs[] is written from the BLE callback (Bluetooth task) and the Wi-Fi
// promiscuous callback (Wi-Fi task), which run on different cores. A spinlock,
// not a mutex: the critical section is a few microseconds of copying, and the
// Wi-Fi task must never wait behind a preempted BLE task. Nothing inside it
// calls FreeRTOS, Serial or the allocator.
static portMUX_TYPE uav_mux = portMUX_INITIALIZER_UNLOCKED;

// Counters for the status line. "No detections" is ambiguous on its own - these
// say whether the radios hear anything, whether Remote ID ever matched, and
// whether the print queue is dropping. Atomic because they are written from
// the BLE task and the Wi-Fi callback and read from the printer task.
std::atomic<uint32_t> stat_wifi_frames(0);
std::atomic<uint32_t> stat_ble_adv(0);
std::atomic<uint32_t> stat_odid_wifi(0);
std::atomic<uint32_t> stat_odid_ble(0);
std::atomic<uint32_t> stat_dji(0);
std::atomic<uint32_t> stat_emitted(0);
std::atomic<uint32_t> stat_queue_drops(0);

// MUST be called inside uav_mux. Finds this MAC's slot, else a free one, else
// recycles the least recently seen - Remote ID MACs rotate, so stale entries
// would otherwise pin the table. A new or recycled slot starts cleared so a
// previous occupant's fields can never bleed into another aircraft.
static id_data* next_uav(const uint8_t* mac, uint32_t now) {
  int idx = -1;
  for (int i = 0; i < MAX_UAVS; i++) {
    if (uavs[i].used && memcmp(uavs[i].mac, mac, 6) == 0) return &uavs[i];
  }
  for (int i = 0; i < MAX_UAVS; i++) {
    if (!uavs[i].used) { idx = i; break; }
  }
  if (idx < 0) {
    uint32_t oldest_age = 0;
    idx = 0;
    for (int i = 0; i < MAX_UAVS; i++) {
      uint32_t age = now - uavs[i].last_seen;       // rollover-safe
      if (age >= oldest_age) { oldest_age = age; idx = i; }
    }
  }
  memset(&uavs[idx], 0, sizeof(uavs[idx]));
  memcpy(uavs[idx].mac, mac, 6);
  uavs[idx].used = true;
  return &uavs[idx];
}

// Merge one decoded update into its slot and hand a snapshot to the printer.
// Called from the BLE and Wi-Fi callbacks, never from an ISR.
static void publish(const id_data &u, uint8_t fields, uint32_t now) {
  id_data snap;
  portENTER_CRITICAL(&uav_mux);
  id_data *s = next_uav(u.mac, now);
  s->rssi = u.rssi;
  s->last_seen = now;
  s->band = u.band;
  s->channel = u.channel;
  s->id_type = u.id_type;
  if (fields & F_ID)   memcpy(s->uav_id, u.uav_id, sizeof(s->uav_id));
  if (fields & F_LOC) {
    s->lat_d = u.lat_d;  s->long_d = u.long_d;
    s->altitude_msl = u.altitude_msl;  s->height_agl = u.height_agl;
    s->speed = u.speed;  s->heading = u.heading;
  }
  if (fields & F_SYS)  { s->base_lat_d = u.base_lat_d;  s->base_long_d = u.base_long_d; }
  if (fields & F_OP)   memcpy(s->op_id, u.op_id, sizeof(s->op_id));
  if (fields & F_HOME) {
    s->home_lat_d = u.home_lat_d;  s->home_long_d = u.home_long_d;
    s->product_type = u.product_type;
  }
  snap = *s;
  portEXIT_CRITICAL(&uav_mux);

  // Never block the radio tasks: a full queue is counted, not waited on.
  if (xQueueSend(printQueue, &snap, 0) == pdTRUE) stat_emitted++;
  else stat_queue_drops++;
}

class MyAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {
public:
  void onResult(BLEAdvertisedDevice device) override {
    int len = device.getPayloadLength();
    if (len <= 0) return;
    stat_ble_adv++;

    uint8_t* payload = device.getPayload();

    // Walk the advertisement's AD structures rather than assuming Remote ID is
    // the first one. A leading Flags record (02 01 06) is legal and common.
    for (int i = 0; i + 1 < len; ) {
      int adLen = payload[i];                      // length counts the type byte
      if (adLen < 1 || i + 1 + adLen > len) break; // malformed or truncated
      // A Remote ID record is 0x16 FA FF 0D <counter> <25-byte message>: the
      // type, UUID, app code and counter take 5 of adLen's bytes and the
      // message the rest. Sized against the record, not the whole payload, so a
      // short lookalike record can't be decoded out of its neighbour's bytes.
      if (payload[i + 1] == 0x16 && adLen >= 5 + ODID_MESSAGE_SIZE &&
          payload[i + 2] == 0xFA && payload[i + 3] == 0xFF &&
          payload[i + 4] == 0x0D) {
        handleOdid(device, &payload[i + 6]);
        break;
      }
      i += adLen + 1;
    }
  }

private:
  void handleOdid(BLEAdvertisedDevice &device, uint8_t *odid) {
    stat_odid_ble++;
    id_data u;
    memset(&u, 0, sizeof(u));
    memcpy(u.mac, device.getAddress().getNative(), 6);
    u.rssi = device.getRSSI();
    u.band = BAND_BLE;
    u.channel = 0;
    u.id_type = ID_ODID;
    uint8_t fields = 0;

    // Decode into a local first; only the merge holds the lock.
    switch (odid[0] & 0xF0) {
      case 0x00: {
        ODID_BasicID_data basic;
        decodeBasicIDMessage(&basic, (ODID_BasicID_encoded*) odid);
        strncpy(u.uav_id, (char*) basic.UASID, ODID_ID_SIZE);
        fields |= F_ID;
        break;
      }
      case 0x10: {
        ODID_Location_data loc;
        decodeLocationMessage(&loc, (ODID_Location_encoded*) odid);
        u.lat_d = loc.Latitude;
        u.long_d = loc.Longitude;
        u.altitude_msl = (int) loc.AltitudeGeo;
        u.height_agl = (int) loc.Height;
        u.speed = (int) loc.SpeedHorizontal;
        u.heading = (int) loc.Direction;
        fields |= F_LOC;
        break;
      }
      case 0x40: {
        ODID_System_data sys;
        decodeSystemMessage(&sys, (ODID_System_encoded*) odid);
        u.base_lat_d = sys.OperatorLatitude;
        u.base_long_d = sys.OperatorLongitude;
        fields |= F_SYS;
        break;
      }
      case 0x50: {
        ODID_OperatorID_data op;
        decodeOperatorIDMessage(&op, (ODID_OperatorID_encoded*) odid);
        strncpy(u.op_id, (char*) op.OperatorId, ODID_ID_SIZE);
        fields |= F_OP;
        break;
      }
    }
    publish(u, fields, millis());
  }
};

// Remote ID from a decoded pack (NAN or beacon) in the global UAS_data.
static void publish_pack(const uint8_t *src_mac, int rssi, uint8_t channel, uint32_t now) {
  id_data u;
  memset(&u, 0, sizeof(u));
  memcpy(u.mac, src_mac, 6);
  u.rssi = rssi;
  u.band = BAND_2_4GHZ;
  u.channel = channel;
  u.id_type = ID_ODID;
  uint8_t fields = 0;
  if (UAS_data.BasicIDValid[0]) {
    strncpy(u.uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
    fields |= F_ID;
  }
  if (UAS_data.LocationValid) {
    u.lat_d = UAS_data.Location.Latitude;
    u.long_d = UAS_data.Location.Longitude;
    u.altitude_msl = (int)UAS_data.Location.AltitudeGeo;
    u.height_agl = (int)UAS_data.Location.Height;
    u.speed = (int)UAS_data.Location.SpeedHorizontal;
    u.heading = (int)UAS_data.Location.Direction;
    fields |= F_LOC;
  }
  if (UAS_data.SystemValid) {
    u.base_lat_d = UAS_data.System.OperatorLatitude;
    u.base_long_d = UAS_data.System.OperatorLongitude;
    fields |= F_SYS;
  }
  if (UAS_data.OperatorIDValid) {
    strncpy(u.op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);
    fields |= F_OP;
  }
  publish(u, fields, now);
}

static void publish_dji(const uint8_t *src_mac, int rssi, uint8_t channel,
                        const dji_droneid_t *d, uint32_t now) {
  id_data u;
  memset(&u, 0, sizeof(u));
  memcpy(u.mac, src_mac, 6);
  u.rssi = rssi;
  u.band = BAND_2_4GHZ;
  u.channel = channel;
  u.id_type = ID_DJI;
  // Position always: 0/0 is DJI's "no fix", and the host treats it as such.
  u.lat_d = d->lat;
  u.long_d = d->lon;
  u.altitude_msl = d->altitude;
  u.height_agl = d->height;
  u.heading = (int)lround(fmod(d->yaw_deg + 360.0, 360.0));
  u.home_lat_d = d->home_lat;
  u.home_long_d = d->home_lon;
  u.product_type = d->product_type;
  uint8_t fields = F_LOC | F_HOME;
  if (d->serial[0]) {
    strncpy(u.uav_id, d->serial, ODID_ID_SIZE);
    fields |= F_ID;
  }
  publish(u, fields, now);
}

void callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  stat_wifi_frames++;

  const wifi_promiscuous_pkt_t *packet = (const wifi_promiscuous_pkt_t *)buffer;
  uint8_t *payload = (uint8_t *)packet->payload;
  // sig_len counts the 4-byte FCS; parse only the frame itself. Anything
  // shorter than a management header has no addresses to read safely.
  int length = (int)packet->rx_ctrl.sig_len - 4;
  if (length < 24) return;
  uint32_t now = millis();
  uint8_t heard_on = current_channel;
  int rssi = packet->rx_ctrl.rssi;

  static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
  if (memcmp(nan_dest, &payload[4], 6) == 0) {
    // The parser copies the frame's source address into this buffer before it
    // checks anything else, so it must be real storage. Upstream passed
    // nullptr, which crashed the node on the first NAN Remote ID frame heard.
    char nan_src[6];
    if (odid_wifi_receive_message_pack_nan_action_frame(&UAS_data, nan_src, payload, length) == 0) {
      stat_odid_wifi++;
      publish_pack(&payload[10], rssi, heard_on, now);
    }
    return;
  }
  if (payload[0] != 0x80) return;                 // beacons only from here

  // Walk the beacon's information elements. Each IE is bounds-checked against
  // the frame before any byte of it is read, and each parser is handed only
  // that IE's body - a truncated IE can't be read past, or into its neighbours.
  for (int off = 36; off + 2 <= length; ) {
    int typ = payload[off];
    int len = payload[off + 1];
    if (off + 2 + len > length) break;            // truncated or malformed
    const uint8_t *body = &payload[off + 2];
    if (typ == 0xdd && len >= 3) {
      if (dji_is_oui(body)) {
        dji_droneid_t dji;
        if (dji_parse(body + 3, len - 3, &dji)) {
          stat_dji++;
          publish_dji(&payload[10], rssi, heard_on, &dji, now);
        }
      } else if ((body[0] == 0x90 && body[1] == 0x3a && body[2] == 0xe6) ||
                 (body[0] == 0xfa && body[1] == 0x0b && body[2] == 0xbc)) {
        // OUI (3) + OUI type (1) + message counter (1), then the message pack.
        // The pack parser reads its own size byte before checking the buffer,
        // so its 3-byte header has to be there before it is called at all.
        int pack_len = len - 5;
        if (pack_len >= 3) {
          memset(&UAS_data, 0, sizeof(UAS_data));
          if (odid_message_process_pack(&UAS_data, (uint8_t *)body + 5, pack_len) >= 0) {
            stat_odid_wifi++;
            publish_pack(&payload[10], rssi, heard_on, now);
          }
        }
      }
    }
    off += len + 2;
  }
}

// ---------------------------------------------------------------------------
// Output. The printer task is the ONLY writer to Serial and Serial1 - the
// status line included - so lines can never interleave.
// ---------------------------------------------------------------------------

// Over-the-air strings go into JSON: escape quote and backslash, and drop
// anything outside printable ASCII so no byte can end the line early or forge
// a second one.
static void json_escape(const char *src, char *dst, size_t n) {
  size_t j = 0;
  for (size_t i = 0; src[i] != '\0' && j + 1 < n; i++) {
    unsigned char c = (unsigned char)src[i];
    if (c == '"' || c == '\\') {
      if (j + 2 >= n) break;
      dst[j++] = '\\';
      dst[j++] = (char)c;
    } else if (c >= 0x20 && c <= 0x7E) {
      dst[j++] = (char)c;
    }
  }
  dst[j] = '\0';
}

static void mac_to_str(const uint8_t *mac, char *out) {
  snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

void send_json_fast(const id_data *UAV) {
  char mac_str[18], id[2 * ODID_ID_SIZE + 1];
  mac_to_str(UAV->mac, mac_str);
  json_escape(UAV->uav_id, id, sizeof(id));
  char json_msg[400];
  if (UAV->id_type == ID_DJI) {
    // DJI DroneID carries a takeoff point, not a live operator position, so it
    // is reported as home_* and never as pilot_*.
    snprintf(json_msg, sizeof(json_msg),
      "{\"mac\":\"%s\",\"rssi\":%d,\"band\":\"%s\",\"channel\":%u,\"id_type\":\"DJI\","
      "\"drone_lat\":%.6f,\"drone_long\":%.6f,\"drone_altitude\":%d,\"height_agl\":%d,"
      "\"heading\":%d,\"home_lat\":%.6f,\"home_long\":%.6f,\"product_type\":%u,"
      "\"basic_id\":\"%s\"}",
      mac_str, UAV->rssi, bandToString(UAV->band), (unsigned) UAV->channel,
      UAV->lat_d, UAV->long_d, UAV->altitude_msl, UAV->height_agl, UAV->heading,
      UAV->home_lat_d, UAV->home_long_d, (unsigned) UAV->product_type, id);
  } else {
    // band and channel say HOW the aircraft was heard. Same field names and
    // order as the C5 build.
    snprintf(json_msg, sizeof(json_msg),
      "{\"mac\":\"%s\",\"rssi\":%d,\"band\":\"%s\",\"channel\":%u,"
      "\"drone_lat\":%.6f,\"drone_long\":%.6f,\"drone_altitude\":%d,"
      "\"pilot_lat\":%.6f,\"pilot_long\":%.6f,\"basic_id\":\"%s\"}",
      mac_str, UAV->rssi, bandToString(UAV->band), (unsigned) UAV->channel,
      UAV->lat_d, UAV->long_d, UAV->altitude_msl,
      UAV->base_lat_d, UAV->base_long_d, id);
  }
  Serial.println(json_msg);
}

// Serial1 carries a short text relay to a Meshtastic radio. It never blocks:
// a line that won't fit in the TX buffer is skipped, not waited for. (Upstream
// slept a full second here, stalling the printer while detections were dropped.)
static void serial1_line(const char *s, int len) {
  if (Serial1.availableForWrite() < len + 2) return;
  Serial1.println(s);
}

void print_compact_message(const id_data *UAV) {
  static unsigned long lastSendTime = 0;
  const unsigned long sendInterval = 5000;
  const int MAX_MESH_SIZE = 230;

  if (millis() - lastSendTime < sendInterval) return;
  lastSendTime = millis();

  char mac_str[18];
  mac_to_str(UAV->mac, mac_str);

  char mesh_msg[MAX_MESH_SIZE];
  int msg_len = snprintf(mesh_msg, sizeof(mesh_msg), "%s: %s RSSI:%d",
                         UAV->id_type == ID_DJI ? "DJI" : "Drone", mac_str, UAV->rssi);
  if (msg_len < MAX_MESH_SIZE && UAV->lat_d != 0.0 && UAV->long_d != 0.0) {
    msg_len += snprintf(mesh_msg + msg_len, sizeof(mesh_msg) - msg_len,
                        " https://maps.google.com/?q=%.6f,%.6f", UAV->lat_d, UAV->long_d);
  }
  serial1_line(mesh_msg, msg_len);

  double plat = UAV->id_type == ID_DJI ? UAV->home_lat_d : UAV->base_lat_d;
  double plon = UAV->id_type == ID_DJI ? UAV->home_long_d : UAV->base_long_d;
  if (plat != 0.0 && plon != 0.0) {
    char pilot_msg[MAX_MESH_SIZE];
    int pilot_len = snprintf(pilot_msg, sizeof(pilot_msg), "%s: https://maps.google.com/?q=%.6f,%.6f",
                             UAV->id_type == ID_DJI ? "Home" : "Pilot", plat, plon);
    serial1_line(pilot_msg, pilot_len);
  }
}

static const char *reset_reason_str(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:   return "power_on";
    case ESP_RST_EXT:       return "external";
    case ESP_RST_SW:        return "software";
    case ESP_RST_PANIC:     return "panic";
    case ESP_RST_INT_WDT:   return "int_watchdog";
    case ESP_RST_TASK_WDT:  return "task_watchdog";
    case ESP_RST_WDT:       return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_BROWNOUT:  return "brownout";
    case ESP_RST_SDIO:      return "sdio";
    default:                return "other";
  }
}

// Valid JSON carrying the counters that tell a wedged node from a quiet sky.
// "reset" says why the node last restarted: "panic" or a watchdog means it
// crashed. The host drops the line for having no "mac" and dims it for saying
// "heartbeat".
static void print_status(uint32_t now) {
  char line[400];
  snprintf(line, sizeof(line),
    "{\"heartbeat\":\"dualcore active\",\"uptime_s\":%lu,\"reset\":\"%s\","
    "\"heap\":%lu,\"min_heap\":%lu,\"wifi_frames\":%lu,\"ble_adv\":%lu,"
    "\"odid_wifi\":%lu,\"odid_ble\":%lu,\"dji\":%lu,\"emitted\":%lu,"
    "\"queue_drops\":%lu,\"channel\":%u}",
    (unsigned long)(now / 1000UL), boot_reason,
    (unsigned long) ESP.getFreeHeap(), (unsigned long) ESP.getMinFreeHeap(),
    (unsigned long) stat_wifi_frames, (unsigned long) stat_ble_adv,
    (unsigned long) stat_odid_wifi, (unsigned long) stat_odid_ble,
    (unsigned long) stat_dji, (unsigned long) stat_emitted,
    (unsigned long) stat_queue_drops, (unsigned) current_channel);
  Serial.println(line);
}

void printerTask(void *param) {
  id_data UAV;
  uint32_t last_status = millis();
  for (;;) {
    if (xQueueReceive(printQueue, &UAV, pdMS_TO_TICKS(1000)) == pdTRUE) {
      send_json_fast(&UAV);
      print_compact_message(&UAV);
    }
    uint32_t now = millis();
    if (now - last_status >= 60000UL) {
      last_status = now;
      print_status(now);
    }
  }
}

void channelHopTask(void *parameter) {
  size_t idx = 0;
  for (;;) {
    current_channel = CHANNEL_HOME;
    esp_wifi_set_channel(CHANNEL_HOME, WIFI_SECOND_CHAN_NONE);
    vTaskDelay(pdMS_TO_TICKS(HOP_DWELL_HOME_MS));

    current_channel = channels_24[idx];
    esp_wifi_set_channel(channels_24[idx], WIFI_SECOND_CHAN_NONE);
    vTaskDelay(pdMS_TO_TICKS(HOP_DWELL_OTHER_MS));
    idx = (idx + 1) % NUM_24_CHANNELS;
  }
}

void bleScanTask(void *parameter) {
  for (;;) {
    pBLEScan->start(1, false);
    pBLEScan->clearResults();
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

void setup() {
  setCpuFrequencyMhz(160);
  Serial.begin(115200);
  // A TX ring buffer so availableForWrite() reflects real headroom for the mesh
  // relay's two lines; must be set before begin().
  Serial1.setTxBufferSize(512);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  boot_reason = reset_reason_str(esp_reset_reason());
  nvs_flash_init();

  // Everything a radio callback touches must exist before the first callback
  // can run. Upstream enabled promiscuous mode first and created the queue
  // after BLE's slow init, so a Remote ID frame heard in between reached a
  // NULL queue and reset the node - repeatedly, near a hovering drone.
  memset(uavs, 0, sizeof(uavs));
  printQueue = xQueueCreate(PRINT_QUEUE_LEN, sizeof(id_data));
  xTaskCreatePinnedToCore(printerTask, "PrinterTask", 10000, NULL, 1, NULL, 1);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  BLEDevice::init("DroneID");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new MyAdvertisedDeviceCallbacks());
  pBLEScan->setActiveScan(true);

  esp_wifi_set_promiscuous_rx_cb(&callback);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(CHANNEL_HOME, WIFI_SECOND_CHAN_NONE);

  xTaskCreatePinnedToCore(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(channelHopTask, "ChannelHopTask", 2048, NULL, 1, NULL, 0);
}

void loop() {
  // All work happens in the tasks above; the status line comes from the
  // printer task so that it can never interleave with a detection line.
  vTaskDelay(pdMS_TO_TICKS(1000));
}
