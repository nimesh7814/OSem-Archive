"use client";

import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Search, Loader2, Download } from 'lucide-react';

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import SensorSelector from './SensorSelector';
import { DetailedSummary } from './AnalysisSummary';
import { CountryRegionSelect } from './CountryRegionSelect';
import { apiUrl } from '@/lib/api';

interface BulkModeViewProps {
  filters: { sensorTypes: string[]; fromDate: string; toDate: string };
  onFilterChange: (filters: any) => void;
  onToggleSensor: (value: string) => void;
  isSearching: boolean;

  searchResult: DetailedSummary | null;
  setSearchResult: (result: DetailedSummary | null) => void;
}

const BulkModeView = ({
  filters,
  onFilterChange,
  onToggleSensor,
  isSearching,

  searchResult,
  setSearchResult,
}: BulkModeViewProps) => {

  const [country, setCountry] = useState('');
  const [region, setRegion] = useState('');
  const [tableData, setTableData] = useState<any[]>([]);
  const [localSearching, setLocalSearching] = useState(false);
  const [initialLoading, setInitialLoading] = useState(false);
  const [downloadingRow, setDownloadingRow] = useState<string | null>(null);

  useEffect(() => {
    // If a search is in progress or a result is already set, don't fetch default data.
    if (localSearching || searchResult) return;

    let isMounted = true;

    const fetchDefaultData = async () => {
      setInitialLoading(true);
      try {
        const res = await fetch(apiUrl('/country_region_data?category=all'));

        if (res.ok && isMounted) {
          const data = await res.json();
          setTableData(data.map((r: any) => ({
            country: r.country,
            region: r.region,
            stations: r.total_stations,
            sensors: r.total_sensors,
            sizeMB: r.estimated_size_mb,
          })));
        }
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
  }, [searchResult]);

  const handleSearch = async () => {
    if (filters.sensorTypes.length === 0) {
      return;
    }

    setLocalSearching(true);
    setSearchResult(null);
    setTableData([]);

    try {
      const params = new URLSearchParams();
      params.append('category', filters.sensorTypes.join(','));

      if (filters.fromDate) params.append('from_date', filters.fromDate);
      if (filters.toDate) params.append('to_date', filters.toDate);
      if (country) params.append('country', country);
      if (region) params.append('region', region);

      const res = await fetch(apiUrl('/country_region_data', params));
      if (!res.ok) throw new Error("API error");

      const data = await res.json();

      const newTableData = data.map((r: any) => ({
        country: r.country,
        region: r.region,
        stations: r.total_stations,
        sensors: r.total_sensors,
        sizeMB: r.estimated_size_mb,
      }));

      setTableData(newTableData);

      const totalStations = newTableData.reduce((acc: number, curr: any) => acc + curr.stations, 0);
      const totalSensors = newTableData.reduce((acc: number, curr: any) => acc + curr.sensors, 0);
      const totalSizeMB = newTableData.reduce((acc: number, curr: any) => acc + curr.sizeMB, 0);

      const size = `${totalSizeMB.toFixed(1)} MB`;
      const range = `${filters.fromDate || '2014-06-03'} to ${filters.toDate || new Date().toISOString().split('T')[0]}`;
      const selectedSensors = filters.sensorTypes;

      setSearchResult({
        records: totalStations * 10,
        size,
        stations: totalStations,
        sensors: totalSensors,
        readings: "N/A",
        range,
        selectedSensors,
      });
    } catch (e) {
      console.error("Failed to query data", e);
    } finally {
      setLocalSearching(false);
    }
  };

  const handleDownload = async (row: any) => {
    const downloadKey = `${row.country}-${row.region}`;
    setDownloadingRow(downloadKey);

    try {
      const params = new URLSearchParams({
        category: 'all',
        from_date: filters.fromDate || '2014-06-03',
        to_date: filters.toDate || new Date().toISOString().split('T')[0],
        country: row.country,
        region: row.region,
        download: 'true',
      });

      const res = await fetch(apiUrl('/country_region_data', params));

      if (!res.ok) throw new Error('Download failed');

      const blob = await res.blob();
      const disposition = res.headers.get('content-disposition');
      const match = disposition?.match(/filename="?([^";]+)"?/i);
      const filename = match?.[1] ?? `${row.country}-${row.region}-country-region-data`;
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => window.URL.revokeObjectURL(url), 0);

    } catch (error) {
      console.error('Failed to download region data', error);
    } finally {
      setDownloadingRow(null);
    }
  };

  const clearSearch = () => {

    setSearchResult(null);
    // Remove the custom queries to refetch the default data
    setTableData([]);
    // Optionally trigger a manual reload of the default data here if needed,
    // but the useEffect will handle it since searchResult goes back to null
  };

  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 20 }}
      className="flex-1 w-full overflow-y-auto"
    >
      <div className="p-8 max-w-6xl mx-auto w-full">
      {/* Header */}
      <div className="space-y-2 mb-10">
        <h2 className="text-4xl font-extrabold text-gray-900 tracking-tight">
          Country / Region Export
        </h2>
        <p className="text-lg text-gray-500 font-medium">
          Download complete datasets by country or region.
        </p>
      </div>

      {/* Search controls */}
      <div className="bg-white p-10 rounded-[40px] shadow-sm border border-gray-100 mb-12 space-y-8">
        <div className="flex flex-col gap-8">
          {/* Sensors + dates */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="space-y-3">
              <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-gray-400">
                Sensors
              </Label>
              <SensorSelector
                selected={filters.sensorTypes}
                onToggle={onToggleSensor}
                className="h-14 w-full bg-[#F8F9FB] border-none rounded-2xl"
              />
            </div>

            <div className="space-y-3">
              <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-gray-400">
                From Date
              </Label>
              <Input
                type="date"
                value={filters.fromDate}
                className="h-14 w-full rounded-2xl bg-[#F8F9FB] border-none text-sm font-bold focus:ring-2 focus:ring-blue-500 pr-10"
                onChange={(e) => onFilterChange({ ...filters, fromDate: e.target.value })}
              />
            </div>

            <div className="space-y-3">
              <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-gray-400">
                To Date
              </Label>
              <Input
                type="date"
                value={filters.toDate}
                className="h-14 w-full rounded-2xl bg-[#F8F9FB] border-none text-sm font-bold focus:ring-2 focus:ring-blue-500 pr-10"
                onChange={(e) => onFilterChange({ ...filters, toDate: e.target.value })}
              />
            </div>
          </div>

          {/* Country / Region + query */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-end">
            <CountryRegionSelect
              country={country}
              region={region}
              onCountryChange={(c) => {
                setCountry(c);
                setRegion('');
              }}
              onRegionChange={setRegion}
              className="lg:col-span-2 w-full"
            />

            <div className="flex gap-3">
              {searchResult && (
                <Button
                  variant="outline"
                  onClick={clearSearch}
                  disabled={localSearching}
                  className="h-14 rounded-2xl px-6 font-black text-gray-600 bg-white shadow-sm border-gray-200 hover:bg-gray-50 flex items-center justify-center transition-all active:scale-95"
                >
                  Clear
                </Button>
              )}
              <Button
                onClick={handleSearch}
                disabled={localSearching || filters.sensorTypes.length === 0}
                className="h-14 w-full rounded-2xl bg-[#2563EB] px-10 font-black text-white shadow-lg shadow-blue-200 transition-all active:scale-95 hover:bg-blue-700 flex items-center justify-center gap-3"
              >

                {localSearching ? (
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

      {/* Data table */}
      {(tableData.length > 0 || initialLoading) && (
        <div className="bg-white rounded-[40px] overflow-hidden shadow-sm border border-gray-100">
          {initialLoading ? (
            <div className="p-10 text-center text-gray-500 font-bold flex flex-col items-center justify-center gap-3">
              <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
              <p>Loading global datasets...</p>
            </div>
          ) : (
            <table className="w-full text-left">
              <thead className="bg-gray-50/50 border-b border-gray-100">
                <tr className="text-xs uppercase text-gray-400 font-black">
                  <th className="px-10 py-6">Country</th>
                  <th className="px-10 py-6">Region</th>
                  <th className="px-10 py-6">Stations</th>
                  <th className="px-10 py-6">Sensors</th>
                  <th className="px-10 py-6">Est. Size</th>
                  <th className="px-10 py-6 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {tableData.map((row, i) => (
                  <tr key={i} className="hover:bg-gray-50/50 transition-colors group">
                    <td className="px-10 py-6 font-black text-gray-900 text-lg tracking-tight">
                      {row.country}
                    </td>
                    <td className="px-10 py-6 text-gray-600 font-bold">
                      {row.region}
                    </td>
                    <td className="px-10 py-6 text-gray-600 font-bold">
                      {row.stations.toLocaleString()}
                    </td>
                    <td className="px-10 py-6 text-gray-600 font-bold">
                      {row.sensors.toLocaleString()}
                    </td>
                    <td className="px-10 py-6 text-gray-600 font-bold">
                      {row.sizeMB.toFixed(2)} MB
                    </td>
                    <td className="px-10 py-6 text-right">
                      <Button
                        variant="default"
                        size="sm"
                        className="rounded-full text-white bg-blue-600 hover:bg-blue-700 font-black px-4 py-1 disabled:opacity-70"
                        onClick={() => handleDownload(row)}
                        disabled={downloadingRow === `${row.country}-${row.region}`}
                      >
                        {downloadingRow === `${row.country}-${row.region}` ? (
                          <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                        ) : (
                          <Download className="w-4 h-4 mr-1" />
                        )}
                        {downloadingRow === `${row.country}-${row.region}` ? 'Downloading' : 'Download'}
                      </Button>

                    </td>
                  </tr>
                ))}
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