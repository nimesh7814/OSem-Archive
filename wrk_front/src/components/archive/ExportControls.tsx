"use client";

import React from "react";
import { Download, Loader2 } from "lucide-react";
import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

interface ExportControlsProps {
  onExport: (format: string) => void;
  selectedFormat: string;
  onFormatChange: (format: "csv" | "geojson") => void;
  disabled?: boolean;
  loading?: boolean;
  loadingLabel?: string | null;
}

const ExportControls = ({
  onExport,
  selectedFormat,
  onFormatChange,
  disabled,
  loading,
  loadingLabel,
}: ExportControlsProps) => {
  const options: Array<{ value: "csv" | "geojson"; label: string }> = [
    { value: "csv", label: "CSV" },
    { value: "geojson", label: "GeoJSON" },
  ];

  return (
    <div className="space-y-4">
      <div className="space-y-3">
        <Label className="text-xs font-semibold text-muted-foreground uppercase tracking-widest ml-1">
          Download Format
        </Label>
        <div className="grid grid-cols-2 gap-2 rounded-2xl bg-muted p-1.5 border border-border">
          {options.map((option) => {
            const active = selectedFormat === option.value;
            return (
              <Button
                key={option.value}
                type="button"
                variant="ghost"
                className={`h-10 rounded-xl font-bold text-xs uppercase tracking-wide transition-colors ${
                  active
                    ? "bg-primary text-primary-foreground hover:bg-primary"
                    : "text-muted-foreground hover:bg-background"
                }`}
                onClick={() => onFormatChange(option.value)}
                disabled={disabled || loading}
              >
                {option.label}
              </Button>
            );
          })}
        </div>
      </div>
      <motion.div
        whileTap={{ scale: loading ? 1 : 0.98 }}
        whileHover={{ y: loading ? 0 : -1 }}
        className="relative"
      >
        {loading && (
          <motion.span
            aria-hidden="true"
            className="absolute inset-0 rounded-2xl bg-primary/10"
            animate={{ opacity: [0.25, 0.55, 0.25] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
        )}
        <Button
          variant="outline"
          className={`relative w-full h-16 overflow-hidden rounded-2xl border-2 font-bold transition-all flex items-center justify-center gap-3 ${loading ? "border-primary bg-primary/10 text-primary shadow-lg shadow-primary/10" : "border-primary/30 bg-primary/5 hover:border-primary hover:bg-primary/10 text-primary"}`}
          onClick={() => onExport(selectedFormat)}
          disabled={disabled || loading}
        >
          {loading ? (
            <Loader2 className="w-5 h-5 animate-spin" />
          ) : (
            <Download className="w-5 h-5" />
          )}
          <span>{loading ? (loadingLabel ?? "Preparing Download") : "Download Data"}</span>
          {loading && (
            <span className="absolute inset-x-0 bottom-0 h-1 bg-primary/40 animate-pulse" />
          )}
        </Button>
      </motion.div>
    </div>
  );
};

export default ExportControls;
