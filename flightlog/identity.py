"""Resolve a detection to a durable drone record.

The legacy app keys aliases, tags and geofence state on MAC. RemoteID MACs are
frequently randomized per power-cycle, so a MAC-keyed label silently stops
following the drone. Here `basic_id` (the RemoteID serial) is authoritative and
MAC is only a fallback, so a label survives a MAC rotation.

Node-mode firmware omits `basic_id` entirely, so a drone seen only through a
node lives as a MAC-keyed placeholder (basic_id NULL) until some build reports
its serial - at which point the placeholder is upgraded in place and keeps all
the flight history it already accumulated.
"""
import threading


class IdentityResolver:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._by_basic = {}     # basic_id -> drone_id
        self._by_mac = {}       # mac      -> drone_id
        self._typed = set()     # drone_ids whose id_type is already recorded
        self._load()

    def _load(self):
        for r in self.db.query("SELECT id, basic_id FROM drones WHERE basic_id IS NOT NULL"):
            self._by_basic[r['basic_id']] = r['id']
        for r in self.db.query("SELECT mac, drone_id FROM drone_macs"):
            self._by_mac[r['mac']] = r['drone_id']

    # -- internals ------------------------------------------------------------
    def _touch_drone(self, drone_id, ts):
        self.db.execute(
            "UPDATE drones SET first_seen=MIN(COALESCE(first_seen,?),?), last_seen=MAX(COALESCE(last_seen,?),?) "
            "WHERE id=?", (ts, ts, ts, ts, drone_id))

    def _attach_mac(self, mac, drone_id, ts):
        """Point `mac` at `drone_id`, moving it off any other drone."""
        if not mac:
            return
        if self._by_mac.get(mac) == drone_id:
            self.db.execute(
                "UPDATE drone_macs SET last_seen=MAX(COALESCE(last_seen,?),?) WHERE mac=?",
                (ts, ts, mac))
            return
        self.db.execute(
            "INSERT INTO drone_macs(mac, drone_id, first_seen, last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(mac) DO UPDATE SET drone_id=excluded.drone_id, "
            "last_seen=MAX(COALESCE(drone_macs.last_seen,excluded.last_seen),excluded.last_seen)",
            (mac, drone_id, ts, ts))
        self._by_mac[mac] = drone_id

    def _create(self, basic_id, mac, ts):
        cur = self.db.execute(
            "INSERT INTO drones(basic_id, first_seen, last_seen) VALUES(?,?,?)",
            (basic_id, ts, ts))
        drone_id = cur.lastrowid
        if basic_id:
            self._by_basic[basic_id] = drone_id
        self._attach_mac(mac, drone_id, ts)
        return drone_id

    # -- public ---------------------------------------------------------------
    def mark_type(self, drone_id, id_type):
        """Record how a drone is heard (e.g. 'DJI'). One write per drone per
        process: detections arrive many times a second."""
        if not id_type or drone_id in self._typed:
            return
        self.db.execute("UPDATE drones SET id_type=? WHERE id=?", (id_type, drone_id))
        self._typed.add(drone_id)

    def resolve(self, mac, basic_id, ts):
        """Return the drone_id for this detection, creating a record if needed."""
        basic_id = (basic_id or None)
        with self._lock:
            if basic_id:
                drone_id = self._by_basic.get(basic_id)
                if drone_id is not None:
                    # Serial wins: if this MAC currently points elsewhere it is
                    # moved here. Two drones with *different* non-null serials
                    # are never merged, even when they share a MAC.
                    self._attach_mac(mac, drone_id, ts)
                    self._touch_drone(drone_id, ts)
                    return drone_id

                # Serial is new. If the MAC already belongs to a placeholder
                # (a drone we have only ever seen without a serial), upgrade it
                # rather than orphaning its history.
                existing = self._by_mac.get(mac)
                if existing is not None:
                    row = self.db.one("SELECT basic_id FROM drones WHERE id=?", (existing,))
                    if row is not None and row['basic_id'] is None:
                        self.db.execute("UPDATE drones SET basic_id=? WHERE id=?",
                                        (basic_id, existing))
                        self._by_basic[basic_id] = existing
                        self._touch_drone(existing, ts)
                        return existing

                return self._create(basic_id, mac, ts)

            # No serial on this detection.
            drone_id = self._by_mac.get(mac)
            if drone_id is not None:
                self._attach_mac(mac, drone_id, ts)
                self._touch_drone(drone_id, ts)
                return drone_id
            return self._create(None, mac, ts)
