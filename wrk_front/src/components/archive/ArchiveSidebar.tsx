"use client";

import React, { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { MousePointer2, Upload, Loader2, Search, Trash2, MapPin, LayoutGrid } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DatePicker } from "@/components/ui/date-picker";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import SensorSelector from "./SensorSelector";
import ExposureSelect from "./ExposureSelect";
import { CountryRegionSelect } from "./CountryRegionSelect";
import AnalysisSummary, { type DetailedSummary } from "./AnalysisSummary";
import ExportControls from "./ExportControls";
import { cn } from "@/lib/utils";
import type { AggregateLevel } from "@/lib/stationApi";

const AGGREGATE_OPTIONS: Array<{ value: AggregateLevel; label: string }> = [
  { value: "raw", label: "Raw" },
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "monthly", label: "Monthly" },
  { value: "yearly", label: "Yearly" },
];

export type ArchivePanel = "region" | "custom";

export type ArchiveFilters = {
  sensorTypes: string[];
  exposure: string;
  fromDate: string;
  toDate: string;
};

interface ArchiveSidebarProps {
  activeTab: ArchivePanel;
  onTabChange: (tab: ArchivePanel) => void;
  filters: ArchiveFilters;
  onFilterChange: (filters: ArchiveFilters) => void;
  onToggleSensor: (value: string | string[]) => void;

  country: string;
  region: string;
  onCountryChange: (country: string) => void;
  onRegionChange: (region: string) => void;

  drawingMode: "none" | "rectangle" | "polygon";
  onToggleDraw: () => void;
  onImportClick: () => void;
  importedFileName: string | null;
  onRemoveImportedFile: () => void;
  hasDrawnAoi: boolean;

  canRun: boolean;
  isSearching: boolean;
  hasRunSearch: boolean;
  searchResult: DetailedSummary | null;
  onRun: () => void;
  onClear: () => void;

  exportFormat: "csv" | "geojson";
  onExportFormatChange: (format: "csv" | "geojson") => void;
  exportAggregate: AggregateLevel;
  onExportAggregateChange: (aggregate: AggregateLevel) => void;
  onExport: () => void;
  isExporting: boolean;
  exportStatusLabel?: string | null;
}

const ArchiveSidebar = ({
  activeTab,
  onTabChange,
  filters,
  onFilterChange,
  onToggleSensor,
  country,
  region,
  onCountryChange,
  onRegionChange,
  drawingMode,
  onToggleDraw,
  onImportClick,
  importedFileName,
  onRemoveImportedFile,
  hasDrawnAoi,
  canRun,
  isSearching,
  hasRunSearch,
  searchResult,
  onRun,
  onClear,
  exportFormat,
  onExportFormatChange,
  exportAggregate,
  onExportAggregateChange,
  onExport,
  isExporting,
  exportStatusLabel,
}: ArchiveSidebarProps) => {
  const resultSectionRef = useRef<HTMLDivElement>(null);
  const hasSearchData =
    !!searchResult &&
    (searchResult.stations > 0 || searchResult.sensors > 0);

  useEffect(() => {
    if (!searchResult) return;
    resultSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [searchResult]);

  return (
    <div className="w-[420px] h-full bg-white border-r border-border flex flex-col z-10 shadow-xl overflow-hidden">
      <ScrollArea className="flex-1 min-h-0">
        <div className="p-8 space-y-8">
          <div>
            <h3 className="text-xl font-semibold mb-1.5 text-foreground tracking-tight">
              Explore the Archive
            </h3>
            <p className="text-sm text-muted-foreground font-medium">
              Pick a country/region or draw an area on the map to retrieve data.
            </p>
          </div>

          <Tabs value={activeTab} onValueChange={(v) => onTabChange(v as ArchivePanel)}>
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="region" className="gap-2">
                <LayoutGrid className="w-3.5 h-3.5" /> Region
              </TabsTrigger>
              <TabsTrigger value="custom" className="gap-2">
                <MapPin className="w-3.5 h-3.5" /> Custom Area
              </TabsTrigger>
            </TabsList>
          </Tabs>

          {activeTab === "region" ? (
            <div className="space-y-3">
              <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
                Country / Region
              </Label>
              <CountryRegionSelect
                country={country}
                region={region}
                onCountryChange={onCountryChange}
                onRegionChange={onRegionChange}
              />
            </div>
          ) : (
            <div className="space-y-3">
              <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
                Area of Interest (AOI)
              </Label>
              <div className="grid grid-cols-2 gap-4">
                <Button
                  variant="outline"
                  className={cn(
                    "h-24 rounded-2xl flex flex-col gap-3 border-dashed border-2 transition-all group",
                    drawingMode !== "none"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary hover:bg-primary/5",
                  )}
                  onClick={onToggleDraw}
                >
                  <MousePointer2
                    className={cn(
                      "w-6 h-6 transition-transform group-hover:scale-110",
                      drawingMode !== "none" ? "text-primary" : "text-muted-foreground",
                    )}
                  />
                  <span className="text-xs font-bold uppercase tracking-widest text-foreground">
                    Draw Area
                  </span>
                </Button>

                <Button
                  variant="outline"
                  className="h-24 rounded-2xl flex flex-col gap-3 border-dashed border-2 border-border hover:border-primary hover:bg-primary/5 transition-all group"
                  onClick={onImportClick}
                >
                  <Upload className="w-6 h-6 text-primary group-hover:scale-110 transition-transform" />
                  <span className="text-xs font-bold uppercase tracking-widest text-foreground">
                    Import File
                  </span>
                </Button>
              </div>
              <p className="text-[11px] text-muted-foreground font-medium ml-1">
                Accepts .kml, .geojson, or a .zip shapefile.
              </p>

              {importedFileName && (
                <div className="flex items-center justify-between rounded-2xl border border-border bg-primary/5 px-4 py-3">
                  <div className="min-w-0">
                    <p className="text-[10px] font-bold uppercase tracking-[0.22em] text-primary">
                      Imported file
                    </p>
                    <p className="mt-1 truncate text-sm font-semibold text-foreground">
                      {importedFileName}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-9 w-9 rounded-full text-primary hover:bg-primary/10"
                    onClick={onRemoveImportedFile}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              )}

              {hasDrawnAoi && !importedFileName && (
                <div className="rounded-2xl border border-border bg-primary/5 px-4 py-3">
                  <p className="text-[10px] font-bold uppercase tracking-[0.22em] text-primary">
                    Area drawn on map
                  </p>
                </div>
              )}
            </div>
          )}

          <div className="space-y-3">
            <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
              Phenomenon
            </Label>
            <SensorSelector
              selected={filters.sensorTypes}
              onToggle={onToggleSensor}
              className="w-full h-11"
            />
          </div>

          <div className="space-y-3">
            <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
              Exposure
            </Label>
            <ExposureSelect
              value={filters.exposure}
              onChange={(exposure) => onFilterChange({ ...filters, exposure })}
            />
          </div>

          <div className="grid grid-cols-2 gap-5">
            <div className="space-y-3">
              <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
                From
              </Label>
              <DatePicker
                value={filters.fromDate}
                onChange={(fromDate) => onFilterChange({ ...filters, fromDate: fromDate ?? filters.fromDate })}
                toDate={new Date()}
              />
            </div>
            <div className="space-y-3">
              <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
                To
              </Label>
              <DatePicker
                value={filters.toDate}
                onChange={(toDate) => onFilterChange({ ...filters, toDate: toDate ?? filters.toDate })}
                toDate={new Date()}
              />
            </div>
          </div>

          <div className="flex gap-3">
            {(hasRunSearch || searchResult) && (
              <Button
                variant="outline"
                onClick={onClear}
                disabled={isSearching}
                className="h-14 rounded-2xl px-6 font-bold"
              >
                Clear
              </Button>
            )}
            <Button
              onClick={onRun}
              disabled={!canRun || isSearching}
              className="w-full h-14 rounded-2xl bg-primary hover:bg-primary/90 text-primary-foreground font-bold text-base shadow-lg shadow-primary/20 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isSearching ? (
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
              ) : (
                <Search className="w-5 h-5 mr-2" />
              )}
              Run Query
            </Button>
          </div>

          <AnimatePresence>
            {hasRunSearch && searchResult && (
              <motion.div
                ref={resultSectionRef}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                className="space-y-8 pt-6 border-t border-border"
              >
                {hasSearchData ? (
                  <>
                    <AnalysisSummary summary={searchResult} />

                    <div className="space-y-3">
                      <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
                        Download Resolution
                      </Label>
                      <Select
                        value={exportAggregate}
                        onValueChange={(v) => onExportAggregateChange(v as AggregateLevel)}
                        disabled={isExporting}
                      >
                        <SelectTrigger className="h-11 w-full rounded-xl">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {AGGREGATE_OPTIONS.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-[11px] text-muted-foreground font-medium ml-1">
                        Raw exports the finest detail and can be a large file; yearly is the smallest.
                      </p>
                    </div>

                    <ExportControls
                      onExport={onExport}
                      selectedFormat={exportFormat}
                      onFormatChange={onExportFormatChange}
                      disabled={isExporting}
                      loading={isExporting}
                      loadingLabel={exportStatusLabel}
                    />
                  </>
                ) : (
                  <div className="rounded-[32px] border border-border bg-muted/50 p-6 text-center shadow-sm">
                    <p className="text-xs font-bold uppercase tracking-[0.24em] text-muted-foreground">
                      No data available
                    </p>
                    <p className="mt-2 text-sm font-medium text-muted-foreground">
                      The selected area and filters returned no matching records.
                    </p>
                  </div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </ScrollArea>
    </div>
  );
};

export default ArchiveSidebar;
