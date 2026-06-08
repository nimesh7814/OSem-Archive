"use client";

import React, { useEffect, useRef, useState } from "react";
import { AnimatePresence } from "framer-motion";
import { useNavigate, useSearchParams } from "react-router-dom";
import ArchiveHeader from "@/components/archive/ArchiveHeader";
import BulkModeView from "@/components/archive/BulkModeView";
import CustomModeView from "@/components/archive/CustomModeView";
import type { AOIShape } from "@/components/archive/ArchiveMap";
import { DetailedSummary } from "@/components/archive/AnalysisSummary";
import {
  fetchStations,
  fetchStationsInBbox,
  type MapStation,
} from "@/lib/stationApi";
import { apiUrl } from "@/lib/api";

const DEFAULT_FROM_DATE = "2014-06-03";

const formatReadings = (value: number) =>
  value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1)}M`
    : value.toLocaleString();

const formatSize = (valueMb: number) =>
  valueMb >= 1024
    ? `${(valueMb / 1024).toFixed(1)} GB`
    : `${valueMb.toFixed(1)} MB`;

const isPolygonCoordinates = (value: unknown): value is [number, number][][] =>
  Array.isArray(value) &&
  Array.isArray(value[0]) &&
  Array.isArray(value[0][0]) &&
  typeof value[0][0][0] === "number" &&
  typeof value[0][0][1] === "number";

const createAoi = (coordinates: [number, number][]): AOIShape | null => {
  if (coordinates.length < 3) return null;

  const ring =
    coordinates[0][0] === coordinates[coordinates.length - 1][0] &&
    coordinates[0][1] === coordinates[coordinates.length - 1][1]
      ? coordinates
      : [...coordinates, coordinates[0]];

  return {
    type: "Feature",
    properties: { source: "polygon" },
    geometry: {
      type: "Polygon",
      coordinates: [ring],
    },
  };
};

const parseGeoJsonAoi = (text: string): AOIShape | null => {
  try {
    const parsed = JSON.parse(text);

    if (
      parsed?.type === "Feature" &&
      parsed.geometry?.type === "Polygon" &&
      isPolygonCoordinates(parsed.geometry.coordinates)
    ) {
      return createAoi(parsed.geometry.coordinates[0]);
    }

    if (
      parsed?.type === "Polygon" &&
      isPolygonCoordinates(parsed.coordinates)
    ) {
      return createAoi(parsed.coordinates[0]);
    }

    if (
      parsed?.type === "FeatureCollection" &&
      Array.isArray(parsed.features)
    ) {
      for (const feature of parsed.features) {
        if (
          feature?.geometry?.type === "Polygon" &&
          isPolygonCoordinates(feature.geometry.coordinates)
        ) {
          return createAoi(feature.geometry.coordinates[0]);
        }
      }
    }
  } catch {
    return null;
  }

  return null;
};

const parseKmlAoi = (text: string): AOIShape | null => {
  try {
    const xml = new DOMParser().parseFromString(text, "application/xml");
    const parserError = xml.querySelector("parsererror");
    if (parserError) return null;

    const polygonNode = xml.querySelector("Polygon");
    const coordinateNode =
      polygonNode?.querySelector("coordinates") ??
      xml.querySelector("coordinates");
    const coordinatesText = coordinateNode?.textContent?.trim();

    if (!coordinatesText) return null;

    const coordinates = coordinatesText
      .split(/\s+/)
      .map((pair) => pair.trim())
      .filter(Boolean)
      .map(
        (pair) => pair.split(",").slice(0, 2).map(Number) as [number, number],
      )
      .filter((pair) => Number.isFinite(pair[0]) && Number.isFinite(pair[1]));

    return createAoi(coordinates);
  } catch {
    return null;
  }
};

const readFileAsText = (file: File) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("Failed to read file"));
    reader.readAsText(file);
  });

const getAoiFromFile = async (file: File) => {
  const fileName = file.name.toLowerCase();
  const text = await readFileAsText(file);

  if (fileName.endsWith(".kml")) {
    return parseKmlAoi(text);
  }

  return parseGeoJsonAoi(text);
};

const ArchivePage = () => {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const initialMode = searchParams.get("mode") === "custom" ? "custom" : "bulk";
  const [mode, setMode] = useState<"bulk" | "custom">(initialMode);
  const [allStations, setAllStations] = useState<MapStation[]>([]);
  const [stations, setStations] = useState<MapStation[]>([]);

  const [mapCenter, setMapCenter] = useState<[number, number]>([0, 0]);
  const [mapZoom, setMapZoom] = useState(2);
  const [aoi, setAoi] = useState<AOIShape | null>(null);
  const [hasAoiInput, setHasAoiInput] = useState(false);
  const [importedFileName, setImportedFileName] = useState<string | null>(null);
  const [aoiAutoZoomToken, setAoiAutoZoomToken] = useState(0);
  const [isLoadingStations, setIsLoadingStations] = useState(true);

  const [filters, setFilters] = useState({
    sensorTypes: [] as string[],
    fromDate: DEFAULT_FROM_DATE,
    toDate: new Date().toISOString().split("T")[0],
  });

  const [isSearching, setIsSearching] = useState(false);
  const [hasRunSearch, setHasRunSearch] = useState(false);
  const [searchResult, setSearchResult] = useState<DetailedSummary | null>(
    null,
  );
  const [isExporting, setIsExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState(0);
  const [currentExportingSensor, setCurrentExportingSensor] = useState<
    string | null
  >(null);

  const canRunCustomSearch = hasAoiInput || !!aoi;

  const clearImportedAoi = () => {
    setAoi(null);
    setImportedFileName(null);
    setHasAoiInput(false);
    setSearchResult(null);
    setHasRunSearch(false);
    setStations(allStations);
  };

  useEffect(() => {
    const loadStations = async () => {
      setIsLoadingStations(true);
      try {
        const data = await fetchStations();
        setAllStations(data);
        setStations(data);
      } catch (error) {
        console.error("Failed to load stations", error);
      } finally {
        setIsLoadingStations(false);
      }
    };

    loadStations();
  }, []);

  useEffect(() => {
    const urlMode = searchParams.get("mode") === "custom" ? "custom" : "bulk";

    if (urlMode !== mode) {
      setMode(urlMode);
    }
  }, [searchParams, mode]);

  useEffect(() => {
    if (searchParams.get("mode") !== mode) {
      setSearchParams({ mode }, { replace: true });
    }

    if (mode === "custom" && allStations.length > 0 && stations.length === 0) {
      setStations(allStations);
    }
  }, [allStations, mode, searchParams, setSearchParams, stations.length]);

  const handleFileImport = async (
    event: React.ChangeEvent<HTMLInputElement>,
  ) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const validExtensions = [".kml", ".json", ".geojson"];
    const fileName = file.name.toLowerCase();
    const isValid = validExtensions.some((ext) => fileName.endsWith(ext));

    if (!isValid) {
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }

    const importedAoi = await getAoiFromFile(file);
    if (!importedAoi) {
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }

    setAoi(importedAoi);
    setImportedFileName(file.name);
    setHasAoiInput(true);
    setAoiAutoZoomToken((prev) => prev + 1);

    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const toggleSensor = (value: string | string[]) => {
    if (Array.isArray(value)) {
      setFilters((prev) => ({ ...prev, sensorTypes: value }));
      return;
    }

    setFilters((prev) => ({
      ...prev,
      sensorTypes: prev.sensorTypes.includes(value)
        ? prev.sensorTypes.filter((t) => t !== value)
        : [...prev.sensorTypes, value],
    }));
  };

  const handleSearch = async (nextAoi?: AOIShape | null) => {
    const activeAoi = nextAoi ?? aoi;
    if (!activeAoi) {
      return;
    }

    setIsSearching(true);
    setSearchResult(null);

    try {
      const data = await fetchStationsInBbox({
        fromDate: filters.fromDate,
        toDate: filters.toDate,
        aoi: JSON.stringify(activeAoi),
        category:
          filters.sensorTypes.length > 0
            ? filters.sensorTypes.join(",")
            : undefined,
      });

      setStations(allStations);

      const totalSensors = data.totalSensors ?? 0;
      const totalReadings = data.totalReadings ?? 0;
      const selectedSensors =
        data.categories?.length > 0
          ? data.categories
          : filters.sensorTypes.length > 0
            ? filters.sensorTypes
            : ["All Sensors"];

      setSearchResult({
        records: totalReadings,
        size: formatSize(data.totalSizeMb),
        stations: data.totalStations,
        sensors: totalSensors,
        readings: formatReadings(totalReadings),
        range: `${filters.fromDate} to ${filters.toDate}`,
        selectedSensors,
      });

      setHasRunSearch(true);
    } catch (error) {
      console.error("Failed to query bbox data", error);
    } finally {
      setIsSearching(false);
    }
  };

  const handleExport = async (format: string, title?: string) => {
    if (!aoi) return;

    setIsExporting(true);
    setCurrentExportingSensor(title ?? "Custom Selection");

    try {
      const params = new URLSearchParams({
        from_date: filters.fromDate,
        to_date: filters.toDate,
        download: "true",
        aoi: JSON.stringify(aoi),
      });

      if (filters.sensorTypes.length > 0) {
        params.set("category", filters.sensorTypes.join(","));
      }
      if (format && format !== "csv") {
        params.set("type", format);
      }

      const response = await fetch(apiUrl("/bbox_data", params));
      if (!response.ok) {
        throw new Error("Failed to download AOI data");
      }

      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition");
      const match = disposition?.match(/filename="?([^";]+)"?/i);
      const filename = match?.[1] ?? "aoi_download.zip";
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => window.URL.revokeObjectURL(url), 0);
    } catch (error) {
      console.error("Export failed", error);
    } finally {
      setIsExporting(false);
      setCurrentExportingSensor(null);
    }
  };

  return (
    <div className="h-screen overflow-hidden bg-[#d4f1db] flex flex-col font-sans antialiased">
      <input
        type="file"
        ref={fileInputRef}
        className="hidden"
        accept=".kml,.json,.geojson"
        onChange={handleFileImport}
      />

      <ArchiveHeader
        mode={mode}
        onModeChange={(nextMode) => {
          setMode(nextMode);
          setHasRunSearch(false);
          clearImportedAoi();
          setSearchParams({ mode: nextMode }, { replace: true });
        }}
        onBack={() => navigate("/")}
      />

      <main className="flex-1 flex overflow-hidden">
        <AnimatePresence mode="wait">
          {mode === "bulk" ? (
            <BulkModeView
              key="bulk"
              filters={filters}
              onFilterChange={setFilters}
              onToggleSensor={toggleSensor}
              isSearching={isSearching}
              searchResult={searchResult}
              setSearchResult={setSearchResult}
            />
          ) : (
            <CustomModeView
              key="custom"
              filters={filters}
              onFilterChange={setFilters}
              onToggleSensor={toggleSensor}
              onSearch={() => handleSearch()}
              onClearResults={() => setSearchResult(null)}
              isSearching={isSearching}
              searchResult={searchResult}
              onExport={handleExport}
              isExporting={isExporting}
              exportProgress={exportProgress}
              currentExportingSensor={currentExportingSensor}
              onImportClick={() => {
                clearImportedAoi();
                fileInputRef.current?.click();
              }}
              importedFileName={importedFileName}
              onRemoveImportedFile={clearImportedAoi}
              mapCenter={mapCenter}
              mapZoom={mapZoom}
              aoi={aoi}
              focusAoiToken={aoiAutoZoomToken}
              hasRunSearch={hasRunSearch}
              canRunSearch={canRunCustomSearch}
              onAOIChange={(nextAoi) => {
                setAoi(nextAoi);
                setHasAoiInput(!!nextAoi);
                if (!nextAoi) {
                  setImportedFileName(null);
                }
              }}
              stations={stations}
              onResetStations={() => setStations(allStations)}
              isLoadingStations={isLoadingStations}
            />
          )}
        </AnimatePresence>
      </main>
    </div>
  );
};

export default ArchivePage;
