"use client";

import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import { ArrowLeft, CalendarDays, MapPin, Waves, CircleSlash2 } from "lucide-react";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, ResponsiveContainer } from "recharts";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import { fetchStationDetails, fetchStations, parseCategories, type MapStation, type StationDetailResponse } from "@/lib/stationApi";

const monthNames = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const MIN_YEAR = 2014;
const MIN_MONTH_INDEX = 5;

const chartPalette = [
  "#2563EB",
  "#10B981",
  "#F59E0B",
  "#8B5CF6",
  "#EF4444",
  "#14B8A6",
  "#F97316",
  "#06B6D4",
];

const makeSensorKey = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, "_");

const formatValue = (value: number) => (Number.isFinite(value) ? value.toFixed(1) : "0.0");
const formatCount = (value: number) => (value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value.toLocaleString());

const getDaysInMonth = (year: number, monthIndex: number) => new Date(year, monthIndex + 1, 0).getDate();

const buildSensorSeries = (detail: StationDetailResponse, category: string, year: number, monthIndex: number) => {
  const daysInMonth = getDaysInMonth(year, monthIndex);
  const chartEntry = detail.chart_data[category] as
    | Array<{ day: number; avg: number }>
    | { data?: Array<{ day: number; avg: number }> }
    | undefined;
  const entries = Array.isArray(chartEntry)
    ? chartEntry
    : Array.isArray(chartEntry?.data)
      ? chartEntry.data
      : [];
  const totalsByDay = new Map<number, { sum: number; count: number }>();

  for (const entry of entries) {
    const current = totalsByDay.get(entry.day) ?? { sum: 0, count: 0 };
    totalsByDay.set(entry.day, {
      sum: current.sum + entry.avg,
      count: current.count + 1,
    });
  }

  const series = Array.from({ length: daysInMonth }, (_, index) => {
    const day = index + 1;
    const totals = totalsByDay.get(day);
    const value = totals ? totals.sum / totals.count : undefined;

    return {
      day,
      label: `Day ${day}`,
      value: value ?? null,
    };
  });

  const hasData = series.some((entry) => entry.value !== null);

  return { series, hasData };
};

const StationReadingsPage = () => {
  const navigate = useNavigate();
  const { stationId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [stationMeta, setStationMeta] = useState<MapStation | null>(null);
  const [detail, setDetail] = useState<StationDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const now = new Date();
  const rawYear = Number(searchParams.get("year") ?? now.getFullYear());
  const year = Number.isNaN(rawYear) ? now.getFullYear() : Math.max(rawYear, MIN_YEAR);
  const rawMonth = Number(searchParams.get("month") ?? (year === MIN_YEAR ? MIN_MONTH_INDEX : now.getMonth()));
  const month = Number.isNaN(rawMonth)
    ? (year === MIN_YEAR ? MIN_MONTH_INDEX : now.getMonth())
    : year === MIN_YEAR && rawMonth < MIN_MONTH_INDEX
      ? MIN_MONTH_INDEX
      : rawMonth;

  useEffect(() => {
    const loadStation = async () => {
      if (!stationId) {
        setStationMeta(null);
        setDetail(null);
        setLoading(false);
        return;
      }

      setLoading(true);
      try {
        const stations = await fetchStations();
        const currentStation = stations.find((item) => item.id === stationId) ?? null;
        setStationMeta(currentStation);

        if (!currentStation) {
          setDetail(null);
          return;
        }

        const backendDetail = await fetchStationDetails({
          stationId,
          month: month + 1,
          year,
        });

        setDetail(backendDetail);
      } catch (error) {
        console.error("Failed to load station details", error);
        setStationMeta(null);
        setDetail(null);
      } finally {
        setLoading(false);
      }
    };

    loadStation();
  }, [month, stationId, year]);

  useEffect(() => {
    const needsUpdate =
      !searchParams.get("month") ||
      !searchParams.get("year") ||
      Number(searchParams.get("year")) < MIN_YEAR ||
      (year === MIN_YEAR && month < MIN_MONTH_INDEX);

    if (needsUpdate) {
      setSearchParams({ month: String(month), year: String(year) }, { replace: true });
    }
  }, [month, searchParams, setSearchParams, year]);

  const station = detail?.station ?? null;
  const sensorTypes = station ? parseCategories(station.categories) : [];
  const sensors = detail?.sensors ?? [];

  const sensorChartData = useMemo(() => {
    if (!detail) return [];

    return sensors.map((sensor, index) => {
      const key = makeSensorKey(sensor.category);
      const { series, hasData } = buildSensorSeries(detail, sensor.category, year, month);

      return {
        key,
        title: sensor.title,
        category: sensor.category,
        color: chartPalette[index % chartPalette.length],
        unit: sensor.unit,
        avg: sensor.avg,
        latest: sensor.latest,
        series,
        hasData,
      };
    });
  }, [detail, month, sensors, year]);

  const sharedYAxisWidth = useMemo(() => {
    const longest = sensorChartData.reduce((max, sensor) => {
      const sensorLongest = sensor.series.reduce((innerMax, entry) => {
        if (entry.value === null) return innerMax;
        return Math.max(innerMax, formatValue(entry.value).length);
      }, 1);

      return Math.max(max, sensorLongest);
    }, 1);

    return Math.max(44, longest * 10 + 16);
  }, [sensorChartData]);

  const updateDate = (key: "month" | "year", value: string) => {

    const nextYear = key === "year" ? Math.max(Number(value), MIN_YEAR) : year;
    const nextMonth = key === "month"
      ? Math.max(Number(value), nextYear === MIN_YEAR ? MIN_MONTH_INDEX : 0)
      : nextYear === MIN_YEAR && month < MIN_MONTH_INDEX
        ? MIN_MONTH_INDEX
        : month;

    setSearchParams({ month: String(nextMonth), year: String(nextYear) });
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#F5F5F7] px-6">
        <Card className="w-full max-w-md rounded-[32px] border-white shadow-2xl">
          <CardContent className="p-8 text-center space-y-4">
            <div className="mx-auto h-14 w-14 rounded-2xl bg-sky-50 flex items-center justify-center animate-pulse">
              <Waves className="w-7 h-7 text-sky-600" />
            </div>
            <h1 className="text-2xl font-black text-slate-900">Loading station</h1>
            <p className="text-sm text-slate-600 font-medium">Fetching station details from the backend.</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!stationMeta) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#F5F5F7] px-6">
        <Card className="w-full max-w-md rounded-[32px] border-white shadow-2xl">
          <CardContent className="p-8 text-center space-y-4">
            <div className="mx-auto h-14 w-14 rounded-2xl bg-rose-50 flex items-center justify-center">
              <MapPin className="w-7 h-7 text-rose-600" />
            </div>
            <h1 className="text-2xl font-black text-slate-900">Station not found</h1>
            <p className="text-sm text-slate-600 font-medium">The station you selected is unavailable.</p>
            <Button className="rounded-xl" onClick={() => navigate("/archive?mode=custom")}>Back to map</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const totalSensors = station?.total_sensors ?? stationMeta.sensors;
  const totalReadings = station?.total_readings ?? (Number(stationMeta.readings.replace(/[^0-9]/g, "")) || 0);

  const renderXAxisProps = (daysInMonth: number) => {
    const rotated = daysInMonth > 15;
    return {
      height: rotated ? 56 : 34,
      tickMargin: rotated ? 18 : 12,
      angle: rotated ? -45 : 0,
      dy: rotated ? 16 : 0,
      textAnchor: rotated ? 'end' as const : 'middle' as const,
      fontSize: daysInMonth > 25 ? 10 : 11,
    };
  };

  return (

    <div className="min-h-screen bg-[#F5F5F7] text-slate-900">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between mb-6">
          <div className="flex items-center gap-3">
            <Button variant="ghost" size="icon" className="rounded-full bg-white shadow-sm border border-slate-200" onClick={() => navigate("/archive?mode=custom")}>
              <ArrowLeft className="w-4 h-4" />
            </Button>
            <div>
              <p className="text-xs font-black uppercase tracking-[0.3em] text-sky-600">Station readings</p>
              <h1 className="text-3xl sm:text-4xl font-black tracking-tight">{stationMeta.name}</h1>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 w-full sm:w-auto sm:grid-cols-2">
            <Select value={String(month)} onValueChange={(value) => updateDate("month", value)}>
              <SelectTrigger className="w-full sm:w-44 rounded-2xl bg-white border-slate-200 shadow-sm h-12">
                <CalendarDays className="w-4 h-4 text-sky-600 mr-2" />
                <SelectValue placeholder="Month" />
              </SelectTrigger>
              <SelectContent>
                {monthNames.map((name, index) => (
                  <SelectItem
                    key={name}
                    value={String(index)}
                    disabled={year === MIN_YEAR && index < MIN_MONTH_INDEX}
                  >
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={String(year)} onValueChange={(value) => updateDate("year", value)}>
              <SelectTrigger className="w-full sm:w-36 rounded-2xl bg-white border-slate-200 shadow-sm h-12">
                <SelectValue placeholder="Year" />
              </SelectTrigger>
              <SelectContent>
                {Array.from({ length: now.getFullYear() - MIN_YEAR + 1 }, (_, index) => now.getFullYear() - index).map((value) => (
                  <SelectItem key={value} value={String(value)}>{value}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="space-y-6">
            <Card className="rounded-[32px] border-white shadow-xl overflow-hidden">
              <div className="h-28 bg-sky-100 relative">
                <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_right,_rgba(59,130,246,0.26),_transparent_42%),radial-gradient(circle_at_bottom_left,_rgba(16,185,129,0.22),_transparent_40%)]" />
              </div>
              <CardContent className="p-6 -mt-10 relative">
                <div className="rounded-[28px] bg-white shadow-lg border border-slate-100 p-5">
                  <div className="flex items-center gap-3 mb-4">
                    <div className="h-12 w-12 rounded-2xl bg-sky-50 flex items-center justify-center">
                      <Waves className="w-6 h-6 text-sky-600" />
                    </div>
                    <div>
                      <p className="text-xs font-black uppercase tracking-[0.3em] text-sky-600">{monthNames[month]} {year}</p>
                      <p className="text-lg font-black text-slate-900">{totalSensors} sensors</p>
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-2">
                    {sensorTypes.map((type) => (
                      <span key={type} className="px-3 py-1.5 rounded-full bg-slate-100 text-xs font-bold text-slate-700">{type}</span>
                    ))}
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="rounded-[32px] border-white shadow-lg">
              <CardHeader className="pb-3">
                <CardTitle className="text-lg font-black tracking-tight">Station details</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm font-medium text-slate-600">
                <div className="flex items-center justify-between"><span>ID</span><span className="font-black text-slate-900">{stationMeta.id}</span></div>
                <Separator />
                <div className="flex items-center justify-between"><span>Location</span><span className="font-black text-slate-900 text-right">{stationMeta.coordinates[1].toFixed(2)}, {stationMeta.coordinates[0].toFixed(2)}</span></div>
                <Separator />
                <div className="flex items-center justify-between"><span>Country</span><span className="font-black text-slate-900 text-right">{station?.country ?? stationMeta.country}</span></div>
                <Separator />
                <div className="flex items-center justify-between"><span>Region</span><span className="font-black text-slate-900 text-right">{station?.region ?? stationMeta.region}</span></div>
                <Separator />
                <div className="flex items-center justify-between"><span>Total readings</span><span className="font-black text-slate-900">{formatCount(totalReadings)}</span></div>

              </CardContent>
            </Card>
          </motion.div>

          <div className="space-y-6">
            {sensorChartData.length === 0 ? (
              <Card className="rounded-[32px] border-white shadow-xl">
                <CardContent className="p-8 sm:p-10 text-center space-y-4">
                  <div className="mx-auto h-16 w-16 rounded-3xl bg-amber-50 flex items-center justify-center">
                    <CircleSlash2 className="w-8 h-8 text-amber-600" />
                  </div>
                  <div className="space-y-2">
                    <h2 className="text-2xl font-black tracking-tight">No sensor data for this period</h2>
                    <p className="text-sm text-slate-600 font-medium max-w-xl mx-auto">
                      The backend returned the station, but no readings for {monthNames[month]} {year}.
                    </p>
                  </div>
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-6">
                {sensorChartData.map((sensor) => (
                  <Card key={sensor.key} className="rounded-[32px] border-white shadow-xl overflow-hidden">
                    <CardHeader className="border-b border-slate-100 bg-white/80 backdrop-blur-sm">
                      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
                        <div>
                          <p className="text-xs font-black uppercase tracking-[0.25em]" style={{ color: sensor.color }}>{sensor.category}</p>
                          <CardTitle className="text-xl font-black tracking-tight">{sensor.title}</CardTitle>
                        </div>
                        <div className="flex flex-wrap gap-2 text-sm font-bold text-slate-500">
                          <span className="rounded-full bg-slate-100 px-3 py-1">Avg {formatValue(sensor.avg)} {sensor.unit}</span>
                        </div>

                      </div>
                    </CardHeader>
                    <CardContent className="p-4 sm:p-6">
                      {sensor.hasData ? (
                        <ChartContainer
                          config={{ [sensor.key]: { label: sensor.title, color: sensor.color } }}
                          className="h-[280px] w-full"
                        >
                          <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={sensor.series} margin={{ top: 10, right: 24, left: 8, bottom: 0 }}>

                              <CartesianGrid vertical={false} strokeDasharray="4 4" stroke="#E2E8F0" />
                              <XAxis
                                dataKey="day"
                                ticks={sensor.series.map((entry) => entry.day)}
                                interval={0}
                                tickLine={false}
                                axisLine={false}
                                stroke="#94A3B8"
                                tickMargin={renderXAxisProps(sensor.series.length).tickMargin}
                                height={renderXAxisProps(sensor.series.length).height}
                                tick={{
                                  fontSize: renderXAxisProps(sensor.series.length).fontSize,
                                  fill: '#94A3B8',
                                }}
                                angle={renderXAxisProps(sensor.series.length).angle}
                                dy={renderXAxisProps(sensor.series.length).dy}
                                textAnchor={renderXAxisProps(sensor.series.length).textAnchor}
                                tickFormatter={(value) => String(value)}
                              />

                              <YAxis
                                tickLine={false}
                                axisLine={false}
                                tickMargin={12}
                                stroke="#94A3B8"
                                width={sharedYAxisWidth}
                                domain={["auto", "auto"]}
                              />

                              <ChartTooltip
                                content={
                                  <ChartTooltipContent
                                    labelFormatter={(_, payload) => `Day ${payload?.[0]?.payload?.day ?? ""}`}
                                  />
                                }
                              />
                              <ChartLegend content={<ChartLegendContent />} />
                              <Line
                                dataKey="value"
                                type="monotone"
                                stroke={sensor.color}
                                strokeWidth={3.25}
                                dot={{ r: 2.5, fill: sensor.color, stroke: '#ffffff', strokeWidth: 1 }}
                                connectNulls
                                activeDot={{ r: 5 }}
                              />

                            </LineChart>
                          </ResponsiveContainer>
                        </ChartContainer>
                      ) : (
                        <div className="h-[280px] rounded-[28px] border border-dashed border-slate-200 bg-slate-50 flex items-center justify-center text-slate-500 font-bold">
                          No data available for this sensor in {monthNames[month]} {year}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default StationReadingsPage;
