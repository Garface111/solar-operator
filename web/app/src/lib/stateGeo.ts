// stateGeo.ts — approximate geographic centroids for US states + DC, and a
// haversine helper. Used to sort the Add-a-client utility list by "nearest to
// you" when the operator grants browser geolocation.
//
// Why centroids, not per-utility coordinates: the provider catalog
// (/v1/providers) carries a two-letter `state` per utility but no lat/lng.
// Distance from the operator's location to each utility's STATE centroid gives
// an honest, useful "near me" ordering (your own state first, then neighbors)
// without inventing precise utility coordinates we don't have.

export type StateCode = string; // two-letter, uppercase

interface LatLng {
  lat: number;
  lng: number;
}

// Geographic centers of each state (and DC). Source: US Census-style state
// centroids, rounded. Good enough for "rank by nearest" — not for routing.
export const STATE_CENTROIDS: Record<StateCode, LatLng> = {
  AL: { lat: 32.8067, lng: -86.7911 },
  AK: { lat: 61.3707, lng: -152.4044 },
  AZ: { lat: 33.7298, lng: -111.4312 },
  AR: { lat: 34.9697, lng: -92.3731 },
  CA: { lat: 36.1162, lng: -119.6816 },
  CO: { lat: 39.0598, lng: -105.3111 },
  CT: { lat: 41.5978, lng: -72.7554 },
  DE: { lat: 39.3185, lng: -75.5071 },
  DC: { lat: 38.8974, lng: -77.0268 },
  FL: { lat: 27.7663, lng: -81.6868 },
  GA: { lat: 33.0406, lng: -83.6431 },
  HI: { lat: 21.0943, lng: -157.4983 },
  ID: { lat: 44.2405, lng: -114.4788 },
  IL: { lat: 40.3495, lng: -88.9861 },
  IN: { lat: 39.8494, lng: -86.2583 },
  IA: { lat: 42.0115, lng: -93.2105 },
  KS: { lat: 38.5266, lng: -96.7265 },
  KY: { lat: 37.6681, lng: -84.6701 },
  LA: { lat: 31.1695, lng: -91.8678 },
  ME: { lat: 44.6939, lng: -69.3819 },
  MD: { lat: 39.0639, lng: -76.8021 },
  MA: { lat: 42.2302, lng: -71.5301 },
  MI: { lat: 43.3266, lng: -84.5361 },
  MN: { lat: 45.6945, lng: -93.9002 },
  MS: { lat: 32.7416, lng: -89.6787 },
  MO: { lat: 38.4561, lng: -92.2884 },
  MT: { lat: 46.9219, lng: -110.4544 },
  NE: { lat: 41.1254, lng: -98.2681 },
  NV: { lat: 38.3135, lng: -117.0554 },
  NH: { lat: 43.4525, lng: -71.5639 },
  NJ: { lat: 40.2989, lng: -74.5210 },
  NM: { lat: 34.8405, lng: -106.2485 },
  NY: { lat: 42.1657, lng: -74.9481 },
  NC: { lat: 35.6301, lng: -79.8064 },
  ND: { lat: 47.5289, lng: -99.7840 },
  OH: { lat: 40.3888, lng: -82.7649 },
  OK: { lat: 35.5653, lng: -96.9289 },
  OR: { lat: 44.5720, lng: -122.0709 },
  PA: { lat: 40.5908, lng: -77.2098 },
  RI: { lat: 41.6809, lng: -71.5118 },
  SC: { lat: 33.8569, lng: -80.9450 },
  SD: { lat: 44.2998, lng: -99.4388 },
  TN: { lat: 35.7478, lng: -86.6923 },
  TX: { lat: 31.0545, lng: -97.5635 },
  UT: { lat: 40.1500, lng: -111.8624 },
  VT: { lat: 44.0459, lng: -72.7107 },
  VA: { lat: 37.7693, lng: -78.1700 },
  WA: { lat: 47.4009, lng: -121.4905 },
  WV: { lat: 38.4912, lng: -80.9545 },
  WI: { lat: 44.2685, lng: -89.6165 },
  WY: { lat: 42.7560, lng: -107.3025 },
};

/** Great-circle distance in miles between two lat/lng points (haversine). */
export function haversineMiles(a: LatLng, b: LatLng): number {
  const R = 3958.8; // Earth radius, miles
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Distance in miles from a point to a state's centroid, or null if the state
 *  code is unknown (e.g. blank/territory). Unknown states sort last. */
export function milesToState(from: LatLng, state: StateCode): number | null {
  const c = STATE_CENTROIDS[(state || "").toUpperCase()];
  if (!c) return null;
  return haversineMiles(from, c);
}
