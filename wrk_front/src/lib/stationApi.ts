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
    from: string | null;
    to: string | null;
  };
  chart_data: Record<
    string,
    Array<{ monthKey: string; label: string; bucket: string; avg: number; min: number; max: number; count: number }> |
    { unit?: string; title?: string; data?: Array<{ monthKey: string; label: string; bucket: string; avg: number; min: number; max: number; count: number }> }
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

export type RegionSummaryRow = {
  country: string;
  region: string;
  total_stations: number;
  total_sensors: number;
  estimated_size_mb: number;
  total_readings: number;
};

export type AggregateLevel = 'raw' | 'hourly' | 'daily' | 'monthly' | 'yearly';

export type ArchiveStats = {
  total_stations: number;
  total_sensors: number;
  total_readings: number;
  total_countries: number;
  updated_at?: string | null;
};

type BackendSensor = {
  _id?: string;
  boxes_id?: string;
  sensorType?: string;
  title?: string;
  unit?: string;
};

type BackendBoxRow = {
  _id: string;
  name?: string;
  country?: string;
  region?: string;
  currentLocation?: {
    coordinates?: [number, number];
  };
  sensors?: BackendSensor[];
  sensor_id?: string;
  sensor_type?: string;
  sensor_title?: string;
  sensor_unit?: string;
  bucket?: string;
  measurementCount?: number;
  avgValue?: number;
  minValue?: number;
  maxValue?: number;
};

type BoxesResponse = {
  summary?: ArchiveStats;
  boxes?: BackendBoxRow[];
};

const jsonHeaders = {
  Accept: 'application/json',
};

const isBoxesResponse = (data: unknown): data is BoxesResponse =>
  !!data && typeof data === 'object' && !Array.isArray(data);

const toRows = (data: unknown): BackendBoxRow[] => {
  if (Array.isArray(data)) return data as BackendBoxRow[];
  if (isBoxesResponse(data) && Array.isArray(data.boxes)) return data.boxes;
  return [];
};

const toSummary = (data: unknown): ArchiveStats | null =>
  isBoxesResponse(data) && data.summary ? data.summary : null;

const uniqueValues = (values: Array<string | undefined>) =>
  Array.from(new Set(values.filter(Boolean) as string[]));

const sensorsForRow = (row: BackendBoxRow): BackendSensor[] => {
  if (Array.isArray(row.sensors)) return row.sensors;
  if (!row.sensor_id && !row.sensor_title && !row.sensor_type) return [];

  return [{
    _id: row.sensor_id,
    sensorType: row.sensor_type,
    title: row.sensor_title,
    unit: row.sensor_unit,
  }];
};

const groupBoxes = (rows: BackendBoxRow[]) => {
  const grouped = new Map<string, {
    first: BackendBoxRow;
    sensors: Map<string, BackendSensor>;
    readings: number;
  }>();

  for (const row of rows) {
    if (!row._id) continue;

    const existing = grouped.get(row._id) ?? {
      first: row,
      sensors: new Map<string, BackendSensor>(),
      readings: 0,
    };

    for (const sensor of sensorsForRow(row)) {
      const key = sensor._id ?? `${sensor.title ?? ''}-${sensor.sensorType ?? ''}`;
      if (key) existing.sensors.set(key, sensor);
    }

    existing.readings += Number(row.measurementCount ?? 0);
    grouped.set(row._id, existing);
  }

  return Array.from(grouped.values());
};

const mapBoxToStation = (box: ReturnType<typeof groupBoxes>[number]): MapStation => {
  const row = box.first;
  const coordinates = row.currentLocation?.coordinates ?? [0, 0];
  const types = uniqueValues(
    Array.from(box.sensors.values()).map((sensor) => sensor.title ?? sensor.sensorType),
  );

  return {
    id: row._id,
    name: row.name,
    coordinates,
    country: row.country,
    region: row.region,
    sensors: box.sensors.size,
    total_sensors: box.sensors.size,
    total_readings: box.readings,
    readings: box.readings ? String(box.readings) : undefined,
    types,
    categories: types.join(', '),
  };
};

const buildFilterParams = ({
  fromDate,
  toDate,
  sensorTypes,
  exposure,
  aggregate,
}: {
  fromDate?: string;
  toDate?: string;
  sensorTypes?: string[];
  exposure?: string;
  aggregate?: AggregateLevel;
}) => {
  const params = new URLSearchParams();
  if (fromDate) params.set('from_date', fromDate);
  if (toDate) params.set('to_date', toDate);
  if (sensorTypes?.length) params.set('phenomenon', sensorTypes.join(','));
  if (exposure) params.set('exposure', exposure);
  params.set('aggregate', aggregate ?? 'monthly');
  return params;
};

const normalizeAoiGeometry = (aoi: string) => {
  const parsed = JSON.parse(aoi);
  return JSON.stringify(parsed?.type === 'Feature' ? parsed.geometry : parsed);
};

const formatMonthLabel = (date: Date) =>
  new Intl.DateTimeFormat('en-US', { month: 'short', year: 'numeric' }).format(date);

const getMonthKey = (date: Date) =>
  `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;

export const parseCategories = (value?: string) =>
  (value ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);

export const fetchStations = async (): Promise<MapStation[]> => {
  const data = await fetchStationsWithSummary();
  return data.stations;
};

export const fetchStationsWithSummary = async (): Promise<{ stations: MapStation[]; summary: ArchiveStats | null }> => {
  const res = await fetch(apiUrl('/boxes'), { headers: jsonHeaders });
  if (!res.ok) throw new Error('Failed to fetch stations');

  const data = await res.json();
  const rows = toRows(data);

  return {
    stations: groupBoxes(rows).map(mapBoxToStation),
    summary: toSummary(data),
  };
};

export const fetchStats = async (): Promise<ArchiveStats> => {
  const res = await fetch(apiUrl('/boxes'), { headers: jsonHeaders });
  if (!res.ok) throw new Error('Failed to fetch stats');

  const summary = toSummary(await res.json());
  if (!summary) throw new Error('Missing archive summary');

  return summary;
};

export const fetchExposures = async (): Promise<string[]> => {
  const res = await fetch(apiUrl('/exposures'), { headers: jsonHeaders });
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data) ? data : [];
};

export const fetchStationInfo = async (stationId: string): Promise<StationInfo | null> => {
  const params = new URLSearchParams({ box_id: stationId });
  const res = await fetch(apiUrl('/boxes', params), { headers: jsonHeaders });

  if (!res.ok) return null;

  const rows = toRows(await res.json());
  const station = groupBoxes(rows).map(mapBoxToStation)[0];
  if (!station) return null;

  return {
    st_id: station.id,
    name: station.name ?? station.id,
    country: station.country ?? '',
    region: station.region ?? '',
    latitude: station.coordinates[1],
    longitude: station.coordinates[0],
    total_sensors: station.total_sensors ?? 0,
    total_readings: station.total_readings ?? 0,
    categories: station.categories ?? '',
  };
};

export const fetchRegionSummaryRows = async ({
  country,
  region,
  fromDate,
  toDate,
  sensorTypes,
  exposure,
}: {
  country?: string;
  region?: string;
  fromDate?: string;
  toDate?: string;
  sensorTypes?: string[];
  exposure?: string;
} = {}): Promise<RegionSummaryRow[]> => {
  const params = buildFilterParams({ fromDate, toDate, sensorTypes, exposure });
  let path = '/boxes';

  if (country || region) {
    path = '/boxes/region';
    if (country) params.set('country', country);
    if (region) params.set('region', region);
  }

  const res = await fetch(apiUrl(path, params), { headers: jsonHeaders });
  if (!res.ok) throw new Error('Failed to fetch region data');

  const rows = toRows(await res.json());
  const summaries = new Map<string, RegionSummaryRow & { boxIds: Set<string>; sensorIds: Set<string> }>();

  for (const row of rows) {
    const countryName = row.country ?? 'Unknown';
    const regionName = row.region ?? 'Unknown';
    const key = `${countryName}::${regionName}`;
    const summary = summaries.get(key) ?? {
      country: countryName,
      region: regionName,
      total_stations: 0,
      total_sensors: 0,
      estimated_size_mb: 0,
      total_readings: 0,
      boxIds: new Set<string>(),
      sensorIds: new Set<string>(),
    };

    if (row._id) summary.boxIds.add(row._id);
    for (const sensor of sensorsForRow(row)) {
      const sensorKey = sensor._id ?? `${sensor.title ?? ''}-${sensor.sensorType ?? ''}`;
      if (sensorKey) summary.sensorIds.add(sensorKey);
    }
    summary.total_readings += Number(row.measurementCount ?? 0);
    summaries.set(key, summary);
  }

  return Array.from(summaries.values()).map((summary) => ({
    country: summary.country,
    region: summary.region,
    total_stations: summary.boxIds.size,
    total_sensors: summary.sensorIds.size,
    total_readings: summary.total_readings,
    estimated_size_mb: Number(((summary.total_readings || summary.sensorIds.size) * 0.00008).toFixed(2)),
  }));
};

export const fetchRegionStations = async ({
  country,
  region,
  fromDate,
  toDate,
  sensorTypes,
  exposure,
}: {
  country?: string;
  region?: string;
  fromDate?: string;
  toDate?: string;
  sensorTypes?: string[];
  exposure?: string;
}): Promise<MapStation[]> => {
  const params = buildFilterParams({ fromDate, toDate, sensorTypes, exposure });
  if (country) params.set('country', country);
  if (region) params.set('region', region);

  const res = await fetch(apiUrl('/boxes/region', params), { headers: jsonHeaders });
  if (!res.ok) throw new Error('Failed to fetch region stations');

  const rows = toRows(await res.json());
  return groupBoxes(rows).map(mapBoxToStation);
};

export type ExportJobStatus = 'pending' | 'running' | 'done' | 'failed';

export type ExportJob = {
  job_id: string;
  status: ExportJobStatus;
  created_at: string;
  file_type: string;
  row_count: number | null;
  error: string | null;
};

const postExportJob = async (params: URLSearchParams, formData: FormData): Promise<ExportJob> => {
  const res = await fetch(apiUrl('/exports', params), {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) throw new Error('Failed to start export job');
  return res.json();
};

export const createRegionExportJob = async ({
  country,
  region,
  fromDate,
  toDate,
  sensorTypes,
  exposure,
  aggregate,
  fileType,
}: {
  country: string;
  region: string;
  fromDate: string;
  toDate: string;
  sensorTypes: string[];
  exposure?: string;
  aggregate?: AggregateLevel;
  fileType: 'csv' | 'geojson';
}): Promise<ExportJob> => {
  const params = buildFilterParams({ fromDate, toDate, sensorTypes, exposure, aggregate });
  params.set('country', country);
  params.set('region', region);
  params.set('file_type', fileType);
  return postExportJob(params, new FormData());
};

export const createAoiExportJob = async ({
  fromDate,
  toDate,
  aoi,
  aoiFile,
  sensorTypes,
  exposure,
  aggregate,
  fileType,
}: {
  fromDate: string;
  toDate: string;
  aoi?: string;
  aoiFile?: File;
  sensorTypes: string[];
  exposure?: string;
  aggregate?: AggregateLevel;
  fileType: 'csv' | 'geojson';
}): Promise<ExportJob> => {
  const params = buildFilterParams({ fromDate, toDate, sensorTypes, exposure, aggregate });
  params.set('file_type', fileType);

  const formData = new FormData();
  if (aoiFile) {
    formData.set('file', aoiFile);
  } else if (aoi) {
    formData.set('geometry', normalizeAoiGeometry(aoi));
  }

  return postExportJob(params, formData);
};

export const getExportJob = async (jobId: string): Promise<ExportJob> => {
  const res = await fetch(apiUrl(`/exports/${jobId}`), { headers: jsonHeaders });
  if (!res.ok) throw new Error('Failed to fetch export job');
  return res.json();
};

export const downloadExportJobFile = async (jobId: string): Promise<Response> => {
  const res = await fetch(apiUrl(`/exports/${jobId}/download`));
  if (!res.ok) throw new Error('Failed to download export file');
  return res;
};

export const fetchStationsInBbox = async ({
  fromDate,
  toDate,
  aoi,
  aoiFile,
  category,
  exposure,
}: {
  fromDate: string;
  toDate: string;
  aoi?: string;
  aoiFile?: File;
  category?: string;
  exposure?: string;
}) => {
  const params = buildFilterParams({
    fromDate,
    toDate,
    sensorTypes: category ? category.split(',').filter(Boolean) : [],
    exposure,
  });
  const formData = new FormData();
  if (aoiFile) {
    formData.set('file', aoiFile);
  } else if (aoi) {
    formData.set('geometry', normalizeAoiGeometry(aoi));
  }

  const res = await fetch(apiUrl('/boxes/aoi', params), {
    method: 'POST',
    body: formData,
  });

  if (!res.ok) throw new Error('Failed to fetch AOI stations');

  const rows = toRows(await res.json());
  const stations = groupBoxes(rows).map(mapBoxToStation);
  const categories = uniqueValues(rows.map((row) => row.sensor_title ?? row.sensor_type));
  const totalReadings = rows.reduce((sum, row) => sum + Number(row.measurementCount ?? 0), 0);

  return {
    totalStations: stations.length,
    totalSensors: new Set(rows.map((row) => row.sensor_id).filter(Boolean)).size,
    totalReadings,
    totalSizeMb: Number(((totalReadings || rows.length) * 0.00008).toFixed(2)),
    categories,
    stations,
  };
};

export const fetchStationDetails = async ({
  stationId,
}: {
  stationId: string;
}): Promise<StationDetailResponse | null> => {
  const params = new URLSearchParams({
    box_id: stationId,
    aggregate: 'monthly',
  });

  const res = await fetch(apiUrl('/boxes', params), { headers: jsonHeaders });
  if (!res.ok) return null;

  const rows = toRows(await res.json());
  const grouped = groupBoxes(rows);
  const station = grouped.map(mapBoxToStation)[0];
  if (!station) return null;

  const chartData: StationDetailResponse['chart_data'] = {};
  const sensorSummaries = new Map<string, {
    se_id: string;
    title: string;
    category: string;
    unit: string;
    weightedSum: number;
    count: number;
    latest: { bucket: string; value: number } | null;
  }>();
  let firstBucket: string | null = null;
  let lastBucket: string | null = null;

  for (const row of rows) {
    const title = row.sensor_title ?? row.sensor_type ?? 'Sensor';
    const bucket = row.bucket ?? '';
    const bucketDate = bucket ? new Date(bucket) : new Date();
    const monthKey = getMonthKey(bucketDate);
    const count = Math.max(Number(row.measurementCount ?? 1), 1);
    const value = Number(row.avgValue ?? 0);

    if (bucket && (!firstBucket || bucket < firstBucket)) firstBucket = bucket;
    if (bucket && (!lastBucket || bucket > lastBucket)) lastBucket = bucket;

    const series = Array.isArray(chartData[title])
      ? chartData[title] as Array<{ monthKey: string; label: string; bucket: string; avg: number; min: number; max: number; count: number }>
      : [];
    series.push({
      monthKey,
      label: formatMonthLabel(bucketDate),
      bucket,
      avg: value,
      min: Number(row.minValue ?? value),
      max: Number(row.maxValue ?? value),
      count,
    });
    chartData[title] = series;

    const summary = sensorSummaries.get(title) ?? {
      se_id: row.sensor_id ?? title,
      title,
      category: title,
      unit: row.sensor_unit ?? '',
      weightedSum: 0,
      count: 0,
      latest: null,
    };
    summary.weightedSum += value * count;
    summary.count += count;
    if (!summary.latest || bucket > summary.latest.bucket) {
      summary.latest = { bucket, value };
    }
    sensorSummaries.set(title, summary);
  }

  return {
    station: {
      st_id: station.id,
      name: station.name ?? station.id,
      latitude: station.coordinates[1],
      longitude: station.coordinates[0],
      total_sensors: station.total_sensors ?? 0,
      total_readings: station.total_readings ?? 0,
      categories: station.categories ?? '',
      country: station.country,
      region: station.region,
    },
    period: { from: firstBucket, to: lastBucket },
    chart_data: chartData,
    sensors: Array.from(sensorSummaries.values()).map((sensor) => {
      const avg = sensor.weightedSum / (sensor.count || 1);
      return {
        se_id: sensor.se_id,
        title: sensor.title,
        category: sensor.category,
        unit: sensor.unit,
        avg,
        latest: sensor.latest?.value ?? 0,
      };
    }),
  };
};

export const buildStationDetailFallback = (station: MapStation, year: number, monthIndex: number) => {
  const types = station.types ?? [];
  const chartData = types.reduce<Record<string, Array<{ monthKey: string; label: string; bucket: string; avg: number; min: number; max: number; count: number }>>>((acc, type, index) => {
    acc[type] = Array.from({ length: 12 }, (_, monthOffset) => {
      const date = new Date(year, monthOffset, 1);
      const base = station.id.length + year + monthOffset + type.length;
      const avg = Number(((base % 120) + index * 3 + 10).toFixed(2));
      return {
        monthKey: getMonthKey(date),
        label: formatMonthLabel(date),
        bucket: date.toISOString(),
        avg,
        min: Math.max(0, avg - 2),
        max: avg + 3,
        count: 1,
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
      from: new Date(year, 0, 1).toISOString(),
      to: new Date(year, 11, 1).toISOString(),
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
