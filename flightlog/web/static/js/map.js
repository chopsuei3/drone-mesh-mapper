/* Shared Leaflet map component.
 *
 * Carries over the tuning from the legacy UI that was clearly profiled:
 * preferCanvas (thousands of path segments render on one canvas rather than
 * one SVG node each), markerZoomAnimation off, and the setPosition patch that
 * pixel-rounds only tile containers - which kills tile seams without making
 * marker motion look stepwise.
 */
(function (global) {
  'use strict';

  // Round tile-container positions only. Lifted from mesh-mapper.py:6670.
  var _setPosition = L.DomUtil.setPosition;
  L.DomUtil.setPosition = function (el, point) {
    if (el && el.classList && el.classList.contains('leaflet-tile-container')) {
      return _setPosition.call(this, el, L.point(Math.round(point.x), Math.round(point.y)));
    }
    return _setPosition.apply(this, arguments);
  };

  // Listed in picker order; the default is chosen by name below, not position.
  var BASEMAPS = {
    'OSM': {
      url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      attribution: 'OpenStreetMap', maxZoom: 19
    },
    'Esri Imagery': {
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      attribution: 'Esri', maxZoom: 19
    },
    'Carto Dark': {
      url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
      attribution: 'CARTO / OpenStreetMap', maxZoom: 20
    }
  };
  var DEFAULT_BASEMAP = 'OSM';

  // Which base layer to open on. Pure so it can be tested without Leaflet. A
  // saved offline layer is not in `available` yet (it arrives async), so it
  // falls back here and is switched to later.
  function pickBasemap(saved, available, fallback) {
    return (saved && available.indexOf(saved) !== -1) ? saved : fallback;
  }
  function loadBasemap() {
    try { return localStorage.getItem('fl.basemap'); } catch (e) { return null; }
  }

  // Colours are resolved on the server (colors.py) and arrive with every row
  // and path; this is only a fallback for a caller that has none. It steps the
  // hue by the golden angle per drone id, like the server, rather than summing
  // character codes - which put flights 100 and 101 one degree apart.
  function hueFor(key) {
    var n = parseInt(key, 10);
    return isNaN(n) ? 0 : Math.round((n * 137.50776405003785) % 360);
  }
  function colorFor(key, given) {
    return given || 'hsl(' + hueFor(key) + ',85%,60%)';
  }

  function MapView(elId, opts) {
    opts = opts || {};
    this.map = L.map(elId, {
      preferCanvas: true,
      markerZoomAnimation: false,
      zoomSnap: 0.25,
      worldCopyJump: true,
      center: opts.center || [20, 0],     // no hard-coded region; see auto-centre below
      zoom: opts.zoom || 2
    });

    var layers = {};
    Object.keys(BASEMAPS).forEach(function (name) {
      var c = BASEMAPS[name];
      layers[name] = L.tileLayer(c.url, { attribution: c.attribution, maxZoom: c.maxZoom });
    });
    // Added before the control exists, so this does not fire baselayerchange
    // and cannot overwrite a saved offline choice we have yet to restore.
    this._base = layers[pickBasemap(loadBasemap(), Object.keys(layers), DEFAULT_BASEMAP)];
    this._base.addTo(this.map);
    this._layerControl = L.control.layers(layers, null, { position: 'topright' }).addTo(this.map);

    var self = this;
    // Fires for radio clicks and for our programmatic restore alike; both mean
    // "this is now the one base layer on the map", so track and remember it.
    this.map.on('baselayerchange', function (e) {
      self._base = e.layer;
      try { localStorage.setItem('fl.basemap', e.name); } catch (err) { /* private mode */ }
    });

    // Offline MBTiles served by /tiles/<name>/{z}/{x}/{y}. These are the same
    // files the legacy app caches into tiles/, so both apps share them.
    fetch('/api/offline_layers').then(function (r) { return r.json(); })
      .then(function (d) {
        (d.layers || []).forEach(function (lyr) {
          if (lyr.vector) return;          // vector needs MapLibre; raster only here
          // Names come from filenames in tiles/, and Leaflet renders layer names
          // and attributions as HTML, so escape them and encode the URL path.
          var safe = String(lyr.name).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
          var t = L.tileLayer('/tiles/' + encodeURIComponent(lyr.name) + '/{z}/{x}/{y}.' +
                              (lyr.format || 'png'), {
            minZoom: lyr.minzoom || 0,
            maxZoom: lyr.maxzoom || 19,
            attribution: 'offline: ' + safe
          });
          var label = '● ' + safe + ' (offline)';
          self._layerControl.addBaseLayer(t, label);
          // Re-read rather than reuse the startup value: if the user picked
          // another layer while this fetch was in flight, that choice wins.
          if (loadBasemap() === label) {
            if (self.map.hasLayer(self._base)) self.map.removeLayer(self._base);
            self.map.addLayer(t);
          }
        });
      }).catch(function () { /* tiles are optional */ });

    this.tracks = L.layerGroup().addTo(this.map);
    this.markers = L.layerGroup().addTo(this.map);
    this._byId = {};
    this._restoreView();
    this.map.on('moveend', function () { self._saveView(); });

    // First visit: open where the data is. Abandoned as soon as the page fits
    // the map to something itself or the user starts dragging.
    this._autoCenter = !this.hasSavedView;
    this.map.on('dragstart', function () { self._autoCenter = false; });
    if (this._autoCenter) {
      fetch('/api/stats').then(function (r) { return r.json(); }).then(function (s) {
        if (self._autoCenter && s && s.center) self.map.setView(s.center, 14);
      }).catch(function () { /* stays on the world view */ });
    }
  }

  MapView.prototype._saveView = function () {
    try {
      var c = this.map.getCenter();
      localStorage.setItem('fl.view', JSON.stringify([c.lat, c.lng, this.map.getZoom()]));
    } catch (e) { /* private mode */ }
  };

  MapView.prototype._restoreView = function () {
    this.hasSavedView = false;
    try {
      var v = JSON.parse(localStorage.getItem('fl.view') || 'null');
      if (v && v.length === 3) {
        this.map.setView([v[0], v[1]], v[2]);
        this.hasSavedView = true;
      }
    } catch (e) { /* ignore */ }
  };

  MapView.prototype.clear = function () {
    this.tracks.clearLayers();
    this.markers.clearLayers();
    this._byId = {};
  };

  /* paths: { flightId: {points:[[lat,lon],..], color} }
     meta:  { flightId: {label, ...} }  used for the popup */
  MapView.prototype.setPaths = function (paths, meta) {
    this.clear();
    meta = meta || {};
    var self = this, bounds = [];
    Object.keys(paths).forEach(function (id) {
      var p = paths[id], pts = p.points || [];
      if (!pts.length) return;
      var color = colorFor(id, p.color);
      var line = L.polyline(pts, { color: color, weight: 2.5, opacity: 0.9 });
      line.addTo(self.tracks);
      var m = meta[id] || {};
      if (m.popup) line.bindPopup(m.popup);
      self._byId[id] = line;
      bounds = bounds.concat(pts);

      // start = hollow ring, end = filled dot. Reads at a glance without a legend.
      L.circleMarker(pts[0], {
        radius: 4, color: color, weight: 2, fillOpacity: 0, pane: 'markerPane'
      }).addTo(self.markers);
      L.circleMarker(pts[pts.length - 1], {
        radius: 4, color: color, weight: 1, fillColor: color, fillOpacity: 1
      }).addTo(self.markers);
    });
    this._bounds = bounds.length ? L.latLngBounds(bounds) : null;
  };

  MapView.prototype.fit = function () {
    if (this._bounds && this._bounds.isValid()) {
      this._autoCenter = false;
      this.map.fitBounds(this._bounds, { padding: [30, 30], maxZoom: 16 });
    }
  };

  MapView.prototype.focus = function (id) {
    var line = this._byId[String(id)];
    if (!line) return false;
    this._autoCenter = false;
    this.map.fitBounds(line.getBounds(), { padding: [40, 40], maxZoom: 16 });
    line.setStyle({ weight: 5 });
    var self = this;
    setTimeout(function () { try { line.setStyle({ weight: 2.5 }); } catch (e) {} }, 900);
    return true;
  };

  MapView.prototype.highlight = function (id, on) {
    var line = this._byId[String(id)];
    if (line) line.setStyle({ weight: on ? 5 : 2.5 });
  };

  MapView.pickBasemap = pickBasemap;   // exposed for tests
  global.MapView = MapView;
  global.colorFor = colorFor;
})(window);
