"use client";

import React, { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { MousePointer2, Upload, Loader2, Search, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import SensorSelector from "./SensorSelector";
import AnalysisSummary, { type DetailedSummary } from "./AnalysisSummary";
import ExportControls from "./ExportControls";
import ArchiveMap, { type AOIShape } from "./ArchiveMap";
import { cn } from "@/lib/utils";
import type { MapStation } from "@/lib/stationApi";

interface CustomModeViewProps {
  filters: { sensorTypes: string[]; fromDate: string; toDate: string };
  onFilterChange: (filters: any) => void;
  onToggleSensor: (value: string) => void;
  onSearch: () => void;
  onClearResults: () => void;
  isSearching: boolean;
  searchResult: DetailedSummary | null;
  onExport: (format: string, title: string) => void;
  isExporting: boolean;
  exportProgress: number;
  currentExportingSensor: string | null;
  onImportClick: () => void;
  importedFileName: string | null;
  onRemoveImportedFile: () => void;
  mapCenter: [number, number];
  mapZoom: number;
  aoi: AOIShape | null;
  focusAoiToken?: number;
  hasRunSearch: boolean;
  canRunSearch: boolean;
  onAOIChange: (aoi: AOIShape | null) => void;
  stations: MapStation[];
  onResetStations: () => void;
  isLoadingStations?: boolean;
}

const CustomModeView = ({
  filters,
  onFilterChange,
  onToggleSensor,
  onSearch,
  onClearResults,
  isSearching,
  searchResult,
  onExport,
  isExporting,
  exportProgress,
  currentExportingSensor,
  onImportClick,
  importedFileName,
  onRemoveImportedFile,
  mapCenter,
  mapZoom,
  aoi,
  hasRunSearch,
  canRunSearch,
  onAOIChange,
  stations,
  onResetStations,
  isLoadingStations,
  focusAoiToken,
}: CustomModeViewProps) => {
  const [drawingMode, setDrawingMode] = useState<"none" | "rectangle">("none");
  const resultSectionRef = useRef<HTMLDivElement>(null);
  const hasSearchData =
    !!searchResult &&
    (searchResult.records > 0 ||
      searchResult.stations > 0 ||
      searchResult.sensors > 0);

  useEffect(() => {
    if (!searchResult) return;

    resultSectionRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }, [searchResult]);

  const handleClear = () => {
    setDrawingMode("none");
    onClearResults();
  };

  const queryDisabled =
    isSearching ||
    !canRunSearch ||
    !filters.sensorTypes.length ||
    !filters.fromDate ||
    !filters.toDate;

  return (
    <motion.div
      initial={{ opacity: 0, x: 20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -20 }}
      className="flex-1 min-h-0 flex overflow-hidden"
    >
      <div className="w-[420px] h-full bg-white border-r border-gray-100 flex flex-col z-10 shadow-xl overflow-hidden">
        <ScrollArea className="flex-1 min-h-0">
          <div className="p-10 space-y-10">
            <div>
              <h3 className="text-2xl font-extrabold mb-2 text-gray-900 tracking-tight">
                Custom Selection
              </h3>
              <p className="text-base text-gray-600 font-medium">
                Define an area to retrieve specific data for particular time
                range.
              </p>
            </div>

            <div className="space-y-8">
              <div className="space-y-3">
                <Label className="text-xs font-bold text-gray-500 uppercase tracking-widest ml-1">
                  Area of Interest (AOI)
                </Label>
                <div className="grid grid-cols-2 gap-4">
                  <Button
                    variant="outline"
                    className={cn(
                      "h-24 rounded-2xl flex flex-col gap-3 border-dashed border-2 transition-all group",
                      drawingMode === "rectangle"
                        ? "border-blue-500 bg-blue-50"
                        : "border-gray-200 hover:border-blue-500 hover:bg-blue-50",
                    )}
                    onClick={() => {
                      const nextMode =
                        drawingMode === "rectangle" ? "none" : "rectangle";

                      if (nextMode === "rectangle" && importedFileName) {
                        onRemoveImportedFile();
                      }

                      setDrawingMode(nextMode);
                    }}
                  >
                    <MousePointer2
                      className={cn(
                        "w-6 h-6 transition-transform group-hover:scale-110",
                        drawingMode === "rectangle"
                          ? "text-blue-600"
                          : "text-gray-400",
                      )}
                    />
                    <span className="text-xs font-extrabold uppercase tracking-widest text-gray-600">
                      Draw Area
                    </span>
                  </Button>

                  <Button
                    variant="outline"
                    className="h-24 rounded-2xl flex flex-col gap-3 border-dashed border-2 border-gray-200 hover:border-indigo-500 hover:bg-indigo-50 transition-all group"
                    onClick={() => {
                      setDrawingMode("none");
                      onImportClick();
                    }}
                  >
                    <Upload className="w-6 h-6 text-indigo-600 group-hover:scale-110 transition-transform" />
                    <span className="text-xs font-extrabold uppercase tracking-widest text-gray-600">
                      Import KML
                    </span>
                  </Button>
                </div>

                {importedFileName && (
                  <div className="flex items-center justify-between rounded-2xl border border-indigo-100 bg-indigo-50/70 px-4 py-3">
                    <div className="min-w-0">
                      <p className="text-[10px] font-black uppercase tracking-[0.22em] text-indigo-500">
                        Imported file
                      </p>
                      <p className="mt-1 truncate text-sm font-bold text-indigo-900">
                        {importedFileName}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-9 w-9 rounded-full text-indigo-700 hover:bg-indigo-100"
                      onClick={onRemoveImportedFile}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                )}
              </div>

              <div className="space-y-3">
                <Label className="text-xs font-bold text-gray-500 uppercase tracking-widest ml-1">
                  Sensors
                </Label>
                <SensorSelector
                  selected={filters.sensorTypes}
                  onToggle={onToggleSensor}
                  className="w-full h-14"
                />
              </div>

              <div className="grid grid-cols-2 gap-5">
                <div className="space-y-3">
                  <Label className="text-xs font-bold text-gray-500 uppercase tracking-widest ml-1">
                    From
                  </Label>
                  <Input
                    type="date"
                    value={filters.fromDate}
                    className="rounded-xl h-14 bg-gray-50 border-none text-sm font-bold focus:ring-2 focus:ring-blue-500"
                    onChange={(e) =>
                      onFilterChange({ ...filters, fromDate: e.target.value })
                    }
                  />
                </div>
                <div className="space-y-3">
                  <Label className="text-xs font-bold text-gray-500 uppercase tracking-widest ml-1">
                    To
                  </Label>
                  <Input
                    type="date"
                    value={filters.toDate}
                    className="rounded-xl h-14 bg-gray-50 border-none text-sm font-bold focus:ring-2 focus:ring-blue-500"
                    onChange={(e) =>
                      onFilterChange({ ...filters, toDate: e.target.value })
                    }
                  />
                </div>
              </div>

              <Button
                onClick={() => {
                  if (queryDisabled) return;
                  onSearch();
                }}
                disabled={queryDisabled}
                className="w-full h-16 rounded-2xl bg-blue-600 hover:bg-blue-700 text-white font-extrabold text-lg shadow-xl shadow-blue-100 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isSearching ? (
                  <Loader2 className="w-6 h-6 animate-spin mr-3" />
                ) : (
                  <Search className="w-6 h-6 mr-3" />
                )}
                Run Query
              </Button>
            </div>

            <AnimatePresence>
              {searchResult && (
                <motion.div
                  ref={resultSectionRef}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="space-y-10 pt-6 border-t border-gray-100"
                >
                  {hasSearchData ? (
                    <>
                      <AnalysisSummary summary={searchResult} />

                      <ExportControls
                        onExport={(format) =>
                          onExport(format, "Custom Selection")
                        }
                        disabled={isExporting}
                        loading={isExporting}
                      />

                      {isExporting && (
                        <div className="flex items-center justify-between rounded-2xl border border-blue-100 bg-blue-50/60 px-4 py-3">
                          <div className="flex items-center gap-3 min-w-0">
                            <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                            <div className="min-w-0">
                              <p className="text-[10px] font-black uppercase tracking-[0.22em] text-blue-500">
                                Downloading
                              </p>
                              <p className="truncate text-sm font-bold text-blue-900">
                                {currentExportingSensor
                                  ? `${currentExportingSensor}...`
                                  : "Preparing export..."}
                              </p>
                            </div>
                          </div>
                          <div className="h-2 w-20 overflow-hidden rounded-full bg-blue-100">
                            <motion.div
                              className="h-full w-full rounded-full bg-blue-500"
                              animate={{ x: ["-60%", "120%"] }}
                              transition={{
                                duration: 1.2,
                                repeat: Infinity,
                                ease: "easeInOut",
                              }}
                            />
                          </div>
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="rounded-[32px] border border-gray-100 bg-gray-50/80 p-6 text-center shadow-sm">
                      <p className="text-xs font-black uppercase tracking-[0.24em] text-gray-400">
                        No data available
                      </p>
                      <p className="mt-2 text-sm font-medium text-gray-600">
                        The selected area and filters returned no matching
                        records.
                      </p>
                    </div>
                  )}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </ScrollArea>
      </div>

      <div className="flex-1 relative min-h-0 overflow-hidden">
        <ArchiveMap
          center={mapCenter}
          zoom={mapZoom}
          fullBleed={true}
          isSearching={isSearching}
          hasResults={!!searchResult}
          hasRunSearch={hasRunSearch}
          drawingMode={drawingMode}
          aoi={aoi}
          focusAoiToken={focusAoiToken}
          onAOIChange={onAOIChange}
          onDrawingModeChange={setDrawingMode}
          onClear={() => {
            handleClear();
            onResetStations();
          }}
          stations={stations}
        />
      </div>
    </motion.div>
  );
};

export default CustomModeView;
