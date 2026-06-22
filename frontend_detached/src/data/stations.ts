export type GeoPoint = [number, number];

export type Station = {
  id: string;
  name: string;
  coordinates: GeoPoint;
  sensors: number;
  types: string[];
  readings: string;
};

export const stations: Station[] = [
  { id: '1', name: 'Colombo Central', coordinates: [79.8612, 6.9271], sensors: 12, types: ['PM2.5', 'PM10', 'Temp', 'Humidity', 'Wind'], readings: '1.2M' },
  { id: '2', name: 'Berlin Mitte', coordinates: [13.4050, 52.5200], sensors: 8, types: ['NO2', 'O3', 'Hum', 'Temp'], readings: '850K' },
  { id: '3', name: 'Mumbai North', coordinates: [72.8777, 19.0760], sensors: 15, types: ['PM2.5', 'CO', 'SO2', 'Temp', 'Wind', 'Humidity'], readings: '2.1M' },
  { id: '4', name: 'New York City', coordinates: [-74.0060, 40.7128], sensors: 24, types: ['Noise', 'UV', 'Press', 'Temp'], readings: '4.5M' },
  { id: '5', name: 'London City', coordinates: [-0.1276, 51.5074], sensors: 10, types: ['PM2.5', 'Temp', 'Rain', 'Humidity', 'Wind'], readings: '1.1M' },
  { id: '6', name: 'Tokyo Hub', coordinates: [139.6503, 35.6762], sensors: 32, types: ['PM2.5', 'PM10', 'NO2', 'O3', 'CO', 'SO2', 'Temp', 'Humidity'], readings: '8.2M' },
];

export const getStationById = (stationId: string) => stations.find((station) => station.id === stationId);

const sensorPalette = [
  'bg-sky-50 text-sky-700 border-sky-200',
  'bg-emerald-50 text-emerald-700 border-emerald-200',
  'bg-amber-50 text-amber-700 border-amber-200',
  'bg-violet-50 text-violet-700 border-violet-200',
  'bg-rose-50 text-rose-700 border-rose-200',
  'bg-cyan-50 text-cyan-700 border-cyan-200',
];

const hashSeed = (value: string) => {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  }
  return hash;
};

const seededNumber = (seed: string, min: number, max: number) => {
  const hash = hashSeed(seed);
  return min + (hash % (max - min + 1));
};

export const getMonthlyStationReadings = (station: Station, year: number, monthIndex: number) => {
  const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();

  return Array.from({ length: daysInMonth }, (_, dayIndex) => {
    const day = dayIndex + 1;

    return {
      day,
      dateLabel: new Date(year, monthIndex, day).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        weekday: 'short',
      }),
      sensors: station.types.map((type, sensorIndex) => {
        const baseSeed = `${station.id}-${year}-${monthIndex}-${day}-${type}`;
        const reading = (seededNumber(baseSeed, 18, 98) + sensorIndex).toFixed(1);
        const hour = seededNumber(`${baseSeed}-hour`, 0, 23);
        const minute = seededNumber(`${baseSeed}-minute`, 0, 59);

        return {
          type,
          reading,
          unit: type.includes('Temp') ? '°C' : type.includes('Hum') ? '%' : type.includes('Wind') ? 'km/h' : type.includes('Press') ? 'hPa' : 'µg/m³',
          time: `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`,
          style: sensorPalette[sensorIndex % sensorPalette.length],
        };
      }),
    };
  });
};
