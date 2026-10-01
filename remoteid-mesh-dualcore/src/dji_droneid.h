/* DJI DroneID over Wi-Fi.
 *
 * Older and Wi-Fi-linked DJI aircraft (Spark, Mavic Air, Mavic Mini and the
 * like) put proprietary telemetry in their Wi-Fi beacons, as a vendor-specific
 * information element (221) under OUI 26:37:12. This is separate from ASTM
 * Remote ID, which current DJI aircraft broadcast as well and which the rest of
 * this firmware already decodes. DJI's OcuSync / O3 / O4 video-link DroneID is
 * a different radio signal altogether and cannot be received by an ESP32.
 *
 * Layout per Kismet's dot11_ie_221_dji_droneid.ksy (decoded by Freek van Tienen
 * and Jan Dumon). Little-endian. Offsets from the first byte after the OUI:
 *
 *   0  vendor type    1-2 unknown    3  subcommand (0x10 = flight telemetry)
 *   4  telemetry record:
 *      +0 version  +1 seq u16  +3 state u16  +5 serial[16]
 *      +21 lon s32  +25 lat s32            (radians x 1e7 - longitude first)
 *      +29 altitude s16  +31 height s16    (metres)
 *      +33 v_north  +35 v_east  +37 v_up   (s16)
 *      +39 pitch  +41 roll  +43 yaw        (s16, hundredths of a degree)
 *      +45 home lon s32  +49 home lat s32  (radians x 1e7)
 *      +53 product type  +54 uuid length  +55 uuid[20] (optional)
 *
 * Subcommand 0x11 carries user-entered "flight purpose" text and is ignored.
 * Everything here is over-the-air input: every read is bounded by the length
 * the caller passes, and nothing is trusted to be printable or in range.
 */
#ifndef DJI_DRONEID_H
#define DJI_DRONEID_H

#include <stdint.h>
#include <string.h>
#include <math.h>

#define DJI_SUBCMD_TELEMETRY   0x10
#define DJI_RECORD_OFFSET      4          /* after vendor type, 2 unknown, subcommand */
#define DJI_RECORD_MIN         55         /* through the uuid length byte */
#define DJI_SERIAL_LEN         16
#define DJI_STATE_SERIAL_VALID 0x0001

typedef struct {
  char     serial[DJI_SERIAL_LEN + 1];    /* printable ASCII only; "" when not valid */
  double   lat, lon;                      /* degrees; 0/0 = no fix */
  double   home_lat, home_lon;            /* takeoff point; 0/0 = not set */
  int16_t  altitude;                      /* metres, as DJI reports it */
  int16_t  height;                        /* metres above the takeoff point */
  int16_t  v_north, v_east, v_up;
  double   yaw_deg;                       /* -180..180 */
  uint16_t state;
  uint8_t  product_type;                  /* DJI's internal model code */
} dji_droneid_t;

static inline int dji_is_oui(const uint8_t *p) {
  return p[0] == 0x26 && p[1] == 0x37 && p[2] == 0x12;
}

static inline uint16_t dji_u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline int16_t  dji_s16(const uint8_t *p) { return (int16_t)dji_u16(p); }
static inline int32_t  dji_s32(const uint8_t *p) {
  return (int32_t)((uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                   ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24));
}

/* Radians x 1e7 to degrees. Exact, rather than the approximate divisor
   174533.0 some decoders use - that one is off by ~2 m at 38 degrees. */
static inline double dji_deg(int32_t raw) { return (double)raw * (180.0 / M_PI) / 1e7; }

/* A position pair outside the globe is corrupt, not a fix: report it as none. */
static inline void dji_sane(double *lat, double *lon) {
  if (!(fabs(*lat) <= 90.0 && fabs(*lon) <= 180.0)) { *lat = 0.0; *lon = 0.0; }
}

/* body: the IE body after the OUI; body_len: its length.
   Returns 1 for a telemetry record, 0 for anything else. */
static inline int dji_parse(const uint8_t *body, int body_len, dji_droneid_t *out) {
  if (body_len < DJI_RECORD_OFFSET + DJI_RECORD_MIN) return 0;
  if (body[3] != DJI_SUBCMD_TELEMETRY) return 0;
  const uint8_t *r = body + DJI_RECORD_OFFSET;

  memset(out, 0, sizeof(*out));
  out->state = dji_u16(r + 3);

  /* The serial only counts when the aircraft flags it valid and every byte up
     to its terminator is printable - a partial or garbled serial would become
     a bogus drone identity on the host. */
  if (out->state & DJI_STATE_SERIAL_VALID) {
    int n = 0;
    for (; n < DJI_SERIAL_LEN && r[5 + n] != 0; n++) {
      if (r[5 + n] < 0x21 || r[5 + n] > 0x7E) { n = -1; break; }
      out->serial[n] = (char)r[5 + n];
    }
    if (n <= 0) out->serial[0] = '\0';
    else out->serial[n] = '\0';
  }

  out->lon      = dji_deg(dji_s32(r + 21));
  out->lat      = dji_deg(dji_s32(r + 25));
  out->altitude = dji_s16(r + 29);
  out->height   = dji_s16(r + 31);
  out->v_north  = dji_s16(r + 33);
  out->v_east   = dji_s16(r + 35);
  out->v_up     = dji_s16(r + 37);
  out->yaw_deg  = dji_s16(r + 43) / 100.0;
  out->home_lon = dji_deg(dji_s32(r + 45));
  out->home_lat = dji_deg(dji_s32(r + 49));
  out->product_type = r[53];
  dji_sane(&out->lat, &out->lon);
  dji_sane(&out->home_lat, &out->home_lon);
  return 1;
}

#endif /* DJI_DRONEID_H */
