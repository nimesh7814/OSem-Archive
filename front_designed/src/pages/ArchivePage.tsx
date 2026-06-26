"use client";

import React, { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import ArchiveHeader from "@/components/archive/ArchiveHeader";
import ArchiveMap, { type AOIShape } from "@/components/archive/ArchiveMap";
import ArchiveSidebar, { type ArchivePanel, type ArchiveFilters } from "@/components/archive/ArchiveSidebar";
import { type DetailedSummary } from "@/components/archive/AnalysisSummary";
import {
  createAoiExportJob,
  createRegionExportJob,
  downloadExportJobFile,
  fetchRegionStations,
  fetchRegionSummaryRows,
  fetchStationsWithSummary,
  fetchStationsInBbox,
  getExportJob,
  type AggregateLevel,
  type ArchiveStats,
  type ExportJob,
  type MapStation,
} from "@/lib/stationApi";

const EXPORT_POLL_INTERVAL_MS = 1500;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const DEFAULT_FROM_DATE = "2014-06-03";
const ALLOWED_AOI_EXTENSIONS = [".kml", ".geojson", ".zip"];

const todayIso = () => new Date().toISOString().split("T")[0];

const toSlashDate = (iso: string) => iso.replaceAll("-", "/");

const formatReadings = (value: number) =>
  value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1)}M`
    : value.toLocaleString();

const formatSize = (valueMb: number) =>
  valueMb >= 1024
    ? `${(valueMb / 1024).toFixed(1)} GB`
    : `${valueMb.toFixed(1)} MB`;

// Best-effort client-side preview of an imported AOI shape, used only to draw
// an outline on the map and zoom to it. The original file (not this parsed
// shape) is still what gets sent to the backend, which parses it robustly
// server-side via geopandas. Shapefile .zip files have no preview here since
// reading them in the browser would need an extra parsing library.
const isPolygonCoordinates = (value: unknown): value is [number, number][][] =>
  Array.isArray(value) &&
  Array.isArray(value[0]) &&
  Array.isArray(value[0][0]) &&
  typeof value[0][0][0] === "number" &&
  typeof value[0][0][1] === "number";

const createPreviewAoi = (coordinates: [number, number][]): AOIShape | null => {
  if (coordinates.length < 3) return null;

  const ring =
    coordinates[0][0] === coordinates[coordinates.length - 1][0] &&
    coordinates[0][1] === coordinates[coordinates.length - 1][1]
      ? coordinates
      : [...coordinates, coordinates[0]];

  return {
    type: "Feature",
    properties: { source: "polygon" },
    geometry: { type: "Polygon", coordinates: [ring] },
  };
};

const parseGeoJsonPreview = (text: string): AOIShape | null => {
  try {
    const parsed = JSON.parse(text);

    if (parsed?.type === "Feature" && parsed.geometry?.type === "Polygon" && isPolygonCoordinates(parsed.geometry.coordinates)) {
      return createPreviewAoi(parsed.geometry.coordinates[0]);
    }
    if (parsed?.type === "Polygon" && isPolygonCoordinates(parsed.coordinates)) {
      return createPreviewAoi(parsed.coordinates[0]);
    }
    if (parsed?.type === "FeatureCollection" && Array.isArray(parsed.features)) {
      for (const feature of parsed.features) {
        if (feature?.geometry?.type === "Polygon" && isPolygonCoordinates(feature.geometry.coordinates)) {
          return createPreviewAoi(feature.geometry.coordinates[0]);
        }
      }
    }
  } catch {
    return null;
  }

  return null;
};

const parseKmlPreview = (text: string): AOIShape | null => {
  try {
    const xml = new DOMParser().parseFromString(text, "application/xml");
    if (xml.querySelector("parsererror")) return null;

    const coordinatesText = xml.querySelector("Polygon coordinates")?.textContent?.trim();
    if (!coordinatesText) return null;

    const coordinates = coordinatesText
      .split(/\s+/)
      .map((pair) => pair.trim())
      .filter(Boolean)
      .map((pair) => pair.split(",").slice(0, 2).map(Number) as [number, number])
      .filter((pair) => Number.isFinite(pair[0]) && Number.isFinite(pair[1]));

    return createPreviewAoi(coordinates);
  } catch {
    return null;
  }
};

const previewAoiFromFile = async (file: File): Promise<AOIShape | null> => {
  const fileName = file.name.toLowerCase();
  if (fileName.endsWith(".zip")) return null;

  const text = await file.text();
  return fileName.endsWith(".kml") ? parseKmlPreview(text) : parseGeoJsonPreview(text);
};

const ArchivePage = () => {
  const [searchParams] = useSearchParams();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [activePanel, setActivePanel] = useState<ArchivePanel>(
    searchParams.get("panel") === "custom" ? "custom" : "region",
  );

  const [allStations, setAllStations] = useState<MapStation[]>([]);
  const [stations, setStations] = useState<MapStation[]>([]);
  const [isLoadingStations, setIsLoadingStations] = useState(true);
  const [archiveStats, setArchiveStats] = useState<ArchiveStats | null>(null);

  const [mapCenter] = useState<[number, number]>([0, 0]);
  const [mapZoom] = useState(2);

  const [country, setCountry] = useState("");
  const [region, setRegion] = useState("");

  const [drawingMode, setDrawingMode] = useState<"none" | "rectangle" | "polygon">("none");
  const [aoi, setAoi] = useState<AOIShape | null>(null);
  const [aoiFile, setAoiFile] = useState<File | null>(null);
  const [importedFileName, setImportedFileName] = useState<string | null>(null);
  const [aoiAutoZoomToken, setAoiAutoZoomToken] = useState(0);
  const [fitStationsToken, setFitStationsToken] = useState(0);

  const [filters, setFilters] = useState<ArchiveFilters>({
    sensorTypes: [],
    exposure: "",
    fromDate: DEFAULT_FROM_DATE,
    toDate: todayIso(),
  });

  const [isSearching, setIsSearching] = useState(false);
  const [hasRunSearch, setHasRunSearch] = useState(false);
  const [searchResult, setSearchResult] = useState<DetailedSummary | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportStatusLabel, setExportStatusLabel] = useState<string | null>(null);
  const [exportFormat, setExportFormat] = useState<"csv" | "geojson">("csv");
  const [exportAggregate, setExportAggregate] = useState<AggregateLevel>("monthly");

  useEffect(() => {
    const loadStations = async () => {
      setIsLoadingStations(true);
      try {
        const data = await fetchStationsWithSummary();
        setAllStations(data.stations);
        setStations(data.stations);
        setArchiveStats(data.summary);
      } catch (error) {
        console.error("Failed to load stations", error);
      } finally {
        setIsLoadingStations(false);
      }
    };

    loadStations();
  }, []);

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

  const handleFileImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const fileName = file.name.toLowerCase();
    const isValid = ALLOWED_AOI_EXTENSIONS.some((ext) => fileName.endsWith(ext));

    if (!isValid) {
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }

    setAoiFile(file);
    setImportedFileName(file.name);
    setDrawingMode("none");

    // Best-effort outline preview so the imported area is visible right away;
    // the raw file above is still what's sent to the backend on Run.
    const preview = await previewAoiFromFile(file);
    setAoi(preview);
    if (preview) setAoiAutoZoomToken((prev) => prev + 1);

    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleAOIChange = (nextAoi: AOIShape | null) => {
    setAoi(nextAoi);
    if (nextAoi) {
      setAoiFile(null);
      setImportedFileName(null);
      setAoiAutoZoomToken((prev) => prev + 1);
    }
  };

  const canRun =
    activePanel === "region" ? !!country : !!aoi || !!aoiFile;

  const handleRun = async () => {
    if (!canRun) return;

    setIsSearching(true);
    setSearchResult(null);

    try {
      if (activePanel === "region") {
        const [summaryRows, resultStations] = await Promise.all([
          fetchRegionSummaryRows({
            country,
            region,
            fromDate: filters.fromDate,
            toDate: filters.toDate,
            sensorTypes: filters.sensorTypes,
            exposure: filters.exposure,
          }),
          fetchRegionStations({
            country,
            region,
            fromDate: filters.fromDate,
            toDate: filters.toDate,
            sensorTypes: filters.sensorTypes,
            exposure: filters.exposure,
          }),
        ]);

        setStations(resultStations);
        setFitStationsToken((prev) => prev + 1);

        const totalStations = summaryRows.reduce((sum, row) => sum + row.total_stations, 0);
        const totalSensors = summaryRows.reduce((sum, row) => sum + row.total_sensors, 0);
        const totalReadings = summaryRows.reduce((sum, row) => sum + row.total_readings, 0);
        const totalSizeMb = summaryRows.reduce((sum, row) => sum + row.estimated_size_mb, 0);

        setSearchResult({
          records: totalReadings,
          size: formatSize(totalSizeMb),
          stations: totalStations,
          sensors: totalSensors,
          readings: formatReadings(totalReadings),
          range: `${toSlashDate(filters.fromDate)} to ${toSlashDate(filters.toDate)}`,
          selectedSensors: filters.sensorTypes.length > 0 ? filters.sensorTypes : ["All Phenomena"],
        });
      } else {
        const data = await fetchStationsInBbox({
          fromDate: filters.fromDate,
          toDate: filters.toDate,
          aoi: aoi ? JSON.stringify(aoi) : undefined,
          aoiFile: aoiFile ?? undefined,
          category: filters.sensorTypes.length > 0 ? filters.sensorTypes.join(",") : undefined,
          exposure: filters.exposure,
        });

        setStations(data.stations);
        if (aoiFile) setFitStationsToken((prev) => prev + 1);

        setSearchResult({
          records: data.totalReadings,
          size: formatSize(data.totalSizeMb),
          stations: data.totalStations,
          sensors: data.totalSensors,
          readings: formatReadings(data.totalReadings),
          range: `${toSlashDate(filters.fromDate)} to ${toSlashDate(filters.toDate)}`,
          selectedSensors:
            data.categories.length > 0
              ? data.categories
              : filters.sensorTypes.length > 0
                ? filters.sensorTypes
                : ["All Phenomena"],
        });
      }

      setHasRunSearch(true);
    } catch (error) {
      console.error("Failed to run query", error);
    } finally {
      setIsSearching(false);
    }
  };

  const handleClear = () => {
    setDrawingMode("none");
    setAoi(null);
    setAoiFile(null);
    setImportedFileName(null);
    setCountry("");
    setRegion("");
    setSearchResult(null);
    setHasRunSearch(false);
    setStations(allStations);
  };

  const handleExport = async () => {
    setIsExporting(true);
    setExportStatusLabel("Queuing export…");

    try {
      let job: ExportJob =
        activePanel === "region"
          ? await createRegionExportJob({
              country,
              region,
              fromDate: filters.fromDate,
              toDate: filters.toDate,
              sensorTypes: filters.sensorTypes,
              exposure: filters.exposure,
              aggregate: exportAggregate,
              fileType: exportFormat,
            })
          : await createAoiExportJob({
              fromDate: filters.fromDate,
              toDate: filters.toDate,
              aoi: aoi ? JSON.stringify(aoi) : undefined,
              aoiFile: aoiFile ?? undefined,
              sensorTypes: filters.sensorTypes,
              exposure: filters.exposure,
              aggregate: exportAggregate,
              fileType: exportFormat,
            });

      while (job.status === "pending" || job.status === "running") {
        setExportStatusLabel(job.status === "pending" ? "Queued for export…" : "Generating file…");
        await sleep(EXPORT_POLL_INTERVAL_MS);
        job = await getExportJob(job.job_id);
      }

      if (job.status === "failed") {
        throw new Error(job.error ?? "Export failed");
      }

      setExportStatusLabel("Downloading…");
      const response = await downloadExportJobFile(job.job_id);
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition");
      const match = disposition?.match(/filename="?([^";]+)"?/i);
      const filename = match?.[1] ?? `osem_archive_export.${exportFormat}`;
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
      setExportStatusLabel(null);
    }
  };

  return (
    <div className="h-screen overflow-hidden bg-secondary flex flex-col font-sans antialiased">
      <input
        type="file"
        ref={fileInputRef}
        className="hidden"
        accept={ALLOWED_AOI_EXTENSIONS.join(",")}
        onChange={handleFileImport}
      />

      <ArchiveHeader stats={archiveStats} />

      <main className="flex-1 flex overflow-hidden">
        <ArchiveSidebar
          activeTab={activePanel}
          onTabChange={(tab) => {
            setActivePanel(tab);
            handleClear();
          }}
          filters={filters}
          onFilterChange={setFilters}
          onToggleSensor={toggleSensor}
          country={country}
          region={region}
          onCountryChange={(nextCountry) => {
            setCountry(nextCountry);
            setRegion("");
          }}
          onRegionChange={setRegion}
          drawingMode={drawingMode}
          onToggleDraw={() => setDrawingMode((prev) => (prev === "none" ? "rectangle" : "none"))}
          onImportClick={() => fileInputRef.current?.click()}
          importedFileName={importedFileName}
          onRemoveImportedFile={() => {
            setAoiFile(null);
            setImportedFileName(null);
            setAoi(null);
          }}
          hasDrawnAoi={!!aoi && !importedFileName}
          canRun={canRun}
          isSearching={isSearching}
          hasRunSearch={hasRunSearch}
          searchResult={searchResult}
          onRun={handleRun}
          onClear={handleClear}
          exportFormat={exportFormat}
          onExportFormatChange={setExportFormat}
          exportAggregate={exportAggregate}
          onExportAggregateChange={setExportAggregate}
          onExport={handleExport}
          isExporting={isExporting}
          exportStatusLabel={exportStatusLabel}
        />

        <div className="flex-1 relative min-h-0 overflow-hidden">
          <ArchiveMap
            center={mapCenter}
            zoom={mapZoom}
            fullBleed
            isSearching={isSearching}
            hasRunSearch={hasRunSearch}
            drawingMode={drawingMode}
            aoi={aoi}
            focusAoiToken={aoiAutoZoomToken}
            fitStationsToken={fitStationsToken}
            onAOIChange={handleAOIChange}
            onDrawingModeChange={setDrawingMode}
            onClear={handleClear}
            stations={stations}
            isLoadingStations={isLoadingStations}
          />
        </div>
      </main>
    </div>
  );
};

export default ArchivePage;
