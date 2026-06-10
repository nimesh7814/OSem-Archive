"use client";

import React, { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Search, Loader2, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import SensorSelector from "./SensorSelector";
import { DetailedSummary } from "./AnalysisSummary";
import { CountryRegionSelect } from "./CountryRegionSelect";
import { apiUrl } from "@/lib/api";

interface BulkModeViewProps {
  filters: ArchiveFilters;
  onFilterChange: (filters: ArchiveFilters) => void;
  onToggleSensor: (value: string) => void;
  isSearching: boolean;
  searchResult: DetailedSummary | null;
  setSearchResult: (result: DetailedSummary | null) => void;
}

type ArchiveFilters = {
  sensorTypes: string[];
  fromDate: string;
  toDate: string;
};

type RegionRow = {
  country: string;
  region: string;
  stations: number;
  sensors: number;
  sizeMB: number;
};

type QuerySummary = {
  sensors: string[];
  country: string;
  region: string;
  fromDate: string;
  toDate: string;
};

type DownloadType = "csv" | "json" | "geojson";

type CountryRegionApiRow = {
  country: string;
  region: string;
  total_stations: number;
  total_sensors: number;
  estimated_size_mb: number;
};

const BulkModeView = ({
  filters,
  onFilterChange,
  onToggleSensor,
  isSearching,
  searchResult,
  setSearchResult,
}: BulkModeViewProps) => {
  const [country, setCountry] = useState("");
  const [region, setRegion] = useState("");
  const [tableData, setTableData] = useState<RegionRow[]>([]);
  const [localSearching, setLocalSearching] = useState(false);
  const [initialLoading, setInitialLoading] = useState(false);
  const [downloadingRow, setDownloadingRow] = useState<string | null>(null);
  const [rowDownloadTypes, setRowDownloadTypes] = useState<
    Record<string, DownloadType>
  >({});
  const [lastQuery, setLastQuery] = useState<QuerySummary | null>(null);

  const getRowDownloadType = (rowKey: string): DownloadType =>
    rowDownloadTypes[rowKey] ?? "csv";

  useEffect(() => {
    if (localSearching || searchResult) return;

    let isMounted = true;

    const fetchDefaultData = async () => {
      setInitialLoading(true);
      try {
        const res = await fetch(apiUrl("/country_region_data"));
        if (!res.ok || !isMounted) return;

        const data = await res.json();
        const rows: CountryRegionApiRow[] = Array.isArray(data)
          ? (data as CountryRegionApiRow[])
          : [];
        setTableData(
          rows.map((row) => ({
            country: row.country,
            region: row.region,
            stations: row.total_stations,
            sensors: row.total_sensors,
            sizeMB: row.estimated_size_mb,
          })),
        );
      } catch (err) {
        console.error("Failed to fetch default data", err);
      } finally {
        if (isMounted) setInitialLoading(false);
      }
    };

    fetchDefaultData();

    return () => {
      isMounted = false;
    };
  }, [localSearching, searchResult]);

  const handleSearch = async () => {
    setLocalSearching(true);
    setSearchResult(null);
    setTableData([]);

    const selectedSensors =
      filters.sensorTypes.length > 0 ? filters.sensorTypes : ["All Sensors"];

    try {
      const params = new URLSearchParams();

      if (filters.sensorTypes.length > 0) {
        params.set("category", filters.sensorTypes.join(","));
      }
      if (filters.fromDate) params.set("from_date", filters.fromDate);
      if (filters.toDate) params.set("to_date", filters.toDate);
      if (country) params.set("country", country);
      if (region) params.set("region", region);

      const res = await fetch(apiUrl("/country_region_data", params));
      if (!res.ok) throw new Error("API error");

      const data = await res.json();
      const rows: CountryRegionApiRow[] = Array.isArray(data)
        ? (data as CountryRegionApiRow[])
        : [];
      const newTableData: RegionRow[] = rows.map((row) => ({
        country: row.country,
        region: row.region,
        stations: row.total_stations,
        sensors: row.total_sensors,
        sizeMB: row.estimated_size_mb,
      }));

      setTableData(newTableData);
      setRowDownloadTypes({});
      setLastQuery({
        sensors: selectedSensors,
        country: country || "All countries",
        region: region || "All regions",
        fromDate: filters.fromDate || "2014-06-03",
        toDate: filters.toDate || new Date().toISOString().split("T")[0],
      });

      const totalStations = newTableData.reduce(
        (acc, row) => acc + row.stations,
        0,
      );
      const totalSensors = newTableData.reduce(
        (acc, row) => acc + row.sensors,
        0,
      );
      const totalSizeMB = newTableData.reduce(
        (acc, row) => acc + row.sizeMB,
        0,
      );

      const formatSize = (mb: number) => {
        if ((mb ?? 0) >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
        return `${(mb ?? 0).toFixed(1)} MB`;
      };

      setSearchResult({
        records: totalStations * 10,
        size: formatSize(totalSizeMB),
        stations: totalStations,
        sensors: totalSensors,
        readings: "N/A",
        range: `${filters.fromDate || "2014-06-03"} to ${filters.toDate || new Date().toISOString().split("T")[0]}`,
        selectedSensors,
      });
    } catch (error) {
      console.error("Failed to query data", error);
    } finally {
      setLocalSearching(false);
    }
  };

  const handleDownload = async (row: RegionRow) => {
    const key = `${row.country}-${row.region}`;
    const rowDownloadType = getRowDownloadType(key);
    setDownloadingRow(key);

    try {
      const params = new URLSearchParams({
        from_date: filters.fromDate || "2014-06-03",
        to_date: filters.toDate || new Date().toISOString().split("T")[0],
        country: row.country,
        region: row.region,
        download: "true",
      });

      if (filters.sensorTypes.length > 0) {
        params.set("category", filters.sensorTypes.join(","));
      }

      if (rowDownloadType !== "csv") {
        params.set("type", rowDownloadType);
      }

      const res = await fetch(apiUrl("/country_region_data", params));
      if (!res.ok) throw new Error("Download failed");

      const blob = await res.blob();
      const disposition = res.headers.get("content-disposition");
      const match = disposition?.match(/filename="?([^";]+)"?/i);
      const filename =
        match?.[1] ?? `${row.country}-${row.region}-country-region-data.zip`;
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => window.URL.revokeObjectURL(url), 0);
    } catch (error) {
      console.error("Failed to download region data", error);
    } finally {
      setDownloadingRow(null);
    }
  };

  const clearSearch = () => {
    setSearchResult(null);
    setTableData([]);
    setLastQuery(null);
  };

  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 20 }}
      className="flex-1 w-full overflow-y-auto"
    >
      <div className="p-8 max-w-6xl mx-auto w-full">
        <div className="space-y-2 mb-10">
          <h2 className="text-4xl font-extrabold text-[#152729] tracking-tight">
            Country / Region Export
          </h2>
          <p className="text-lg text-[#152729]/60 font-medium">
            Download complete datasets by country or region.
          </p>
        </div>

        <div className="bg-white p-10 rounded-[40px] shadow-sm border border-[#d4f1db] mb-12 space-y-8">
          <div className="flex flex-col gap-8">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div className="space-y-3">
                <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-[#152729]/50">
                  Sensors
                </Label>
                <SensorSelector
                  selected={filters.sensorTypes}
                  onToggle={onToggleSensor}
                  className="h-14 w-full bg-[#d4f1db]/30 border-none rounded-2xl"
                />
              </div>

              <div className="space-y-3">
                <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-[#152729]/50">
                  From Date
                </Label>
                <Input
                  type="date"
                  value={filters.fromDate}
                  className="h-14 w-full rounded-2xl bg-[#d4f1db]/30 border-none text-sm font-bold focus:ring-2 focus:ring-[#387218] pr-10"
                  onChange={(e) =>
                    onFilterChange({ ...filters, fromDate: e.target.value })
                  }
                />
              </div>

              <div className="space-y-3">
                <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-[#152729]/50">
                  To Date
                </Label>
                <Input
                  type="date"
                  value={filters.toDate}
                  className="h-14 w-full rounded-2xl bg-[#d4f1db]/30 border-none text-sm font-bold focus:ring-2 focus:ring-[#387218] pr-10"
                  onChange={(e) =>
                    onFilterChange({ ...filters, toDate: e.target.value })
                  }
                />
              </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-end">
              <CountryRegionSelect
                country={country}
                region={region}
                onCountryChange={(nextCountry) => {
                  setCountry(nextCountry);
                  setRegion("");
                }}
                onRegionChange={setRegion}
                className="lg:col-span-2 w-full"
              />

              <div className="flex gap-3">
                {searchResult && (
                  <Button
                    variant="outline"
                    onClick={clearSearch}
                    disabled={localSearching || isSearching}
                    className="h-14 rounded-2xl px-6 font-black text-[#152729] bg-white shadow-sm border-[#d4f1db] hover:bg-[#d4f1db]/30 flex items-center justify-center transition-all active:scale-95"
                  >
                    Clear
                  </Button>
                )}

                <Button
                  onClick={handleSearch}
                  disabled={localSearching || isSearching}
                  className="h-14 w-full rounded-2xl bg-[#387218] px-10 font-black text-white shadow-lg shadow-[#d4f1db] transition-all active:scale-95 hover:bg-[#2d5a12] flex items-center justify-center gap-3"
                >
                  {localSearching || isSearching ? (
                    <Loader2 className="w-5 h-5 animate-spin" />
                  ) : (
                    <Search className="w-5 h-5" />
                  )}
                  Query
                </Button>
              </div>
            </div>
          </div>
        </div>

        {lastQuery && (
          <div className="mb-8 rounded-[32px] border border-[#d4f1db] bg-white/80 p-6 shadow-sm">
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-[10px] font-black uppercase tracking-[0.24em] text-[#387218]">
                    Last Query
                  </p>
                  <p className="mt-1 text-sm font-bold text-[#152729]/70">
                    The table below reflects this query only after pressing
                    Query.
                  </p>
                </div>
                <Badge className="rounded-full bg-[#d4f1db] text-[#387218] hover:bg-[#d4f1db] border border-[#387218]/20 px-3 py-1 font-bold">
                  Saved
                </Badge>
              </div>

              <div className="flex flex-wrap gap-2">
                <Badge
                  variant="secondary"
                  className="rounded-full bg-[#d4f1db] text-[#152729] border-none px-3 py-1 font-bold"
                >
                  {lastQuery.country}
                </Badge>
                <Badge
                  variant="secondary"
                  className="rounded-full bg-[#d4f1db] text-[#152729] border-none px-3 py-1 font-bold"
                >
                  {lastQuery.region}
                </Badge>
                <Badge
                  variant="secondary"
                  className="rounded-full bg-[#152729]/10 text-[#152729] border-none px-3 py-1 font-bold"
                >
                  {lastQuery.fromDate} → {lastQuery.toDate}
                </Badge>
                {lastQuery.sensors.slice(0, 5).map((sensor) => (
                  <Badge
                    key={sensor}
                    variant="secondary"
                    className="rounded-full bg-[#387218]/15 text-[#387218] border-none px-3 py-1 font-bold"
                  >
                    {sensor}
                  </Badge>
                ))}
                {lastQuery.sensors.length > 5 && (
                  <Badge
                    variant="secondary"
                    className="rounded-full bg-[#387218]/25 text-[#152729] border-none px-3 py-1 font-bold"
                  >
                    +{lastQuery.sensors.length - 5}
                  </Badge>
                )}
              </div>
            </div>
          </div>
        )}

        {(tableData.length > 0 || initialLoading) && (
          <div className="bg-white rounded-[40px] overflow-hidden shadow-sm border border-[#d4f1db]">
            {initialLoading ? (
              <div className="p-10 text-center text-[#152729]/60 font-bold flex flex-col items-center justify-center gap-3">
                <Loader2 className="w-8 h-8 animate-spin text-[#387218]" />
                <p>Loading global datasets...</p>
              </div>
            ) : (
              <table className="w-full text-left">
                <thead className="bg-[#d4f1db]/30 border-b border-[#d4f1db]">
                  <tr className="text-xs uppercase text-[#152729]/50 font-black">
                    <th className="px-10 py-6">Country</th>
                    <th className="px-10 py-6">Region</th>
                    <th className="px-10 py-6 text-right">Stations</th>
                    <th className="px-10 py-6 text-right">Sensors</th>
                    <th className="px-6 py-6 text-center w-[220px]">
                      Download Format
                    </th>
                    <th className="pl-4 pr-12 py-6 text-right w-[190px]">
                      Action
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#d4f1db]/50">
                  {tableData.map((row) => {
                    const rowKey = `${row.country}-${row.region}`;

                    return (
                      <tr
                        key={rowKey}
                        className="hover:bg-[#d4f1db]/20 transition-colors group"
                      >
                        <td className="px-10 py-6 font-black text-[#152729] text-lg tracking-tight">
                          {row.country}
                        </td>
                        <td className="px-10 py-6 text-[#152729]/70 font-bold">
                          {row.region}
                        </td>
                        <td className="px-10 py-6 text-[#152729]/70 font-bold text-right">
                          {row.stations.toLocaleString()}
                        </td>
                        <td className="px-10 py-6 text-[#152729]/70 font-bold text-right">
                          {row.sensors.toLocaleString()}
                        </td>
                        <td className="px-6 py-6 text-center">
                          <select
                            aria-label="Download format"
                            className="h-10 min-w-[160px] rounded-full border border-[#d4f1db] bg-[#d4f1db]/30 px-4 text-sm font-bold text-[#152729] outline-none transition-colors focus:border-[#387218]"
                            value={getRowDownloadType(rowKey)}
                            onChange={(e) =>
                              setRowDownloadTypes((prev) => ({
                                ...prev,
                                [rowKey]: e.target.value as DownloadType,
                              }))
                            }
                            disabled={downloadingRow === rowKey}
                          >
                            <option value="csv">csv</option>
                            <option value="json">json</option>
                            <option value="geojson">geojson</option>
                          </select>
                        </td>
                        <td className="pl-4 pr-12 py-6 text-right">
                          <div className="inline-flex items-center">
                            <Button
                              variant="default"
                              size="sm"
                              className="w-[140px] justify-center rounded-full bg-[#387218] px-0 py-1 text-sm font-black text-white hover:bg-[#2d5a12] disabled:opacity-70"
                              onClick={() => handleDownload(row)}
                              disabled={downloadingRow === rowKey}
                            >
                              {downloadingRow === rowKey ? (
                                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                              ) : (
                                <Download className="w-4 h-4 mr-1" />
                              )}
                              {downloadingRow === rowKey
                                ? "Downloading"
                                : "Download"}
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </motion.div>
  );
};

export default BulkModeView;
