import { apiUrl } from './api';

export type GeoPoint = [number, number];

export type MapStation = {
  id: string;
  coordinates: GeoPoint;
  name?: string;
  country?: string;
  region?: string;
  sensors?: number;
  types?: string[];
  readings?: string;
  total_sensors?: number;
  total_readings?: number;
  categories?: string;
};

export type StationInfo = {
  st_id: string;
  name: string;
  country: string;
  region: string;
  latitude: number;
  longitude: number;
  total_sensors: number;
  total_readings: number;
  categories: string;
};

export type StationDetailResponse = {
  station: {
    st_id: string;
    name: string;
    latitude: number;
    longitude: number;
    total_sensors: number;
    total_readings: number;
    categories: string;
    country?: string;
    region?: string;
  };
  period: {
    month: number;
    year: number;
  };
  chart_data: Record<
    string,
    Array<{ day: number; avg: number; min: number; max: number }> | { unit?: string; title?: string; data?: Array<{ day: number; avg: number; min: number; max: number }> }
  >;
  sensors: Array<{
    se_id: string;
    title: string;
    category: string;
    unit: string;
    avg: number;
    latest: number;
  }>;
};

export const parseCategories = (value?: string) =>
  (value ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);

export const fetchStations = async (): Promise<MapStation[]> => {
  const res = await fetch(apiUrl('/get_stations'));
  if (!res.ok) throw new Error('Failed to fetch stations');

  const data = await res.json();
  const stations = Array.isArray(data) ? data : data.stations ?? [];

  return stations.map((station: { st_id: string; name?: string; latitude: number; longitude: number }) => ({
    id: station.st_id,
    name: station.name,
    coordinates: [station.longitude, station.latitude],
  }));
};

export const fetchStationInfo = async (stationId: string): Promise<StationInfo | null> => {
  const params = new URLSearchParams({ station_id: stationId });
  const res = await fetch(apiUrl('/get_stations', params));

  if (!res.ok) return null;
  return res.json();
};

export const fetchStationsInBbox = async ({
  fromDate,
  toDate,
  aoi,
}: {
  fromDate: string;
  toDate: string;
  aoi: string;
}) => {
  const params = new URLSearchParams({
    from_date: fromDate,
    to_date: toDate,
    aoi,
  });

  const res = await fetch(apiUrl('/bbox_data', params));

  if (!res.ok) throw new Error('Failed to fetch bbox stations');

  const data = await res.json();
  const summary = data.summary ?? data;
  const rows = data.data ?? data.stations ?? [];

  return {
    area: data.area,
    period: data.period,
    totalStations: summary.total_stations ?? 0,
    totalSensors: summary.total_sensors ?? 0,
    totalReadings: summary.total_readings ?? 0,
    totalSizeMb: summary.estimated_size ?? summary.estimated_size_mb ?? 0,
    categories: data.categories ?? [],
    stations: Array.isArray(rows)
      ? rows.map((station: { st_id: string; lat?: number; lng?: number; latitude?: number; longitude?: number; total_sensors?: number; total_readings?: number; total_files?: number; categories?: string; country?: string; region?: string; name?: string }) => ({
          id: station.st_id,
          coordinates: [station.longitude ?? station.lng ?? 0, station.latitude ?? station.lat ?? 0],
          name: station.name,
          country: station.country,
          region: station.region,
          sensors: station.total_sensors,
          readings: String(station.total_readings ?? station.total_files ?? 0),
          categories: station.categories,
        }))
      : [],
  };
};

export const fetchStationDetails = async ({
  stationId,
  month,
  year,
}: {
  stationId: string;
  month: number;
  year: number;
}): Promise<StationDetailResponse | null> => {
  const params = new URLSearchParams({
    st_id: stationId,
    month: String(month),
    year: String(year),
  });

  const res = await fetch(apiUrl('/station_readings', params));

  if (!res.ok) return null;

  return res.json();
};

export const buildStationDetailFallback = (station: MapStation, year: number, monthIndex: number) => {
  const types = station.types ?? [];
  const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
  const chartData = types.reduce<Record<string, Array<{ day: number; avg: number; min: number; max: number }>>>((acc, type, index) => {
    acc[type] = Array.from({ length: Math.min(daysInMonth, 8) }, (_, dayIndex) => {
      const day = dayIndex + 1 + index;
      const base = station.id.length + year + monthIndex + day + type.length;
      const avg = Number(((base % 120) + index * 3 + 10).toFixed(2));
      return {
        day,
        avg,
        min: Math.max(0, avg - 2),
        max: avg + 3,
      };
    });
    return acc;
  }, {});

  return {
    station: {
      st_id: station.id,
      name: station.name ?? station.id,
      latitude: station.coordinates[1],
      longitude: station.coordinates[0],
      total_sensors: station.sensors ?? station.total_sensors ?? types.length,
      total_readings: Number((station.readings ?? String(station.total_readings ?? 0)).replace(/[^0-9]/g, '')) || 0,
      categories: types.join(', '),
    },
    period: {
      month: monthIndex + 1,
      year,
    },
    chart_data: chartData,
    sensors: types.map((type, index) => ({
      se_id: `${station.id}-${type}`,
      title: type,
      category: type,
      unit: index === 0 ? 'unit' : 'value',
      avg: chartData[type]?.reduce((sum, item) => sum + item.avg, 0) / (chartData[type]?.length || 1),
      latest: chartData[type]?.[chartData[type].length - 1]?.avg ?? 0,
    })),
  } as StationDetailResponse;
};
