"use client";

import React from 'react';
import { CheckCircle2, Radio, Database, Activity, Info, Calendar as CalendarIcon } from 'lucide-react';
import { Badge } from "@/components/ui/badge";

export interface DetailedSummary {
  records: number;
  size: string;
  stations: number;
  sensors: number;
  readings: string;
  range: string;
  selectedSensors: string[];
}

interface AnalysisSummaryProps {
  summary: DetailedSummary;
}

const AnalysisSummary = ({ summary }: AnalysisSummaryProps) => {
  return (
    <div className="bg-primary/5 p-6 rounded-[32px] border border-primary/10 space-y-6">
      <h4 className="text-xs font-bold text-primary uppercase tracking-[0.2em] flex items-center">
        <CheckCircle2 className="w-4 h-4 mr-2" /> Data Selected
      </h4>

      <div className="grid grid-cols-2 gap-y-6 gap-x-4">
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase font-semibold tracking-wider">
            <Radio className="w-3.5 h-3.5 text-primary" /> Stations
          </div>
          <p className="text-xl font-bold text-foreground">{summary.stations}</p>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase font-semibold tracking-wider">
            <Database className="w-3.5 h-3.5 text-primary" /> Sensors
          </div>
          <p className="text-xl font-bold text-foreground">{summary.sensors}</p>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase font-semibold tracking-wider">
            <Activity className="w-3.5 h-3.5 text-primary" /> Readings
          </div>
          <p className="text-xl font-bold text-foreground">{summary.readings}</p>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase font-semibold tracking-wider">
            <Info className="w-3.5 h-3.5 text-primary" /> Est. Size
          </div>
          <p className="text-xl font-bold text-foreground">{summary.size}</p>
        </div>
      </div>

      <div className="pt-4 border-t border-primary/10">
        <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase font-semibold tracking-wider mb-2">
          <CalendarIcon className="w-3.5 h-3.5 text-primary" /> Date Range
        </div>
        <p className="text-sm font-bold text-foreground">{summary.range}</p>
      </div>

      <div className="pt-4 border-t border-primary/10">
        <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase font-semibold tracking-wider mb-3">
          <Activity className="w-3.5 h-3.5 text-primary" /> Selected Phenomena
        </div>
        <div className="flex flex-wrap gap-2">
          {summary.selectedSensors.slice(0, 7).map(s => (
            <Badge key={s} variant="secondary" className="text-[11px] px-3 py-1 font-semibold bg-primary/10 text-primary border-none rounded-lg">
              {s}
            </Badge>
          ))}
          {summary.selectedSensors.length > 7 && (
            <Badge variant="secondary" className="text-[11px] px-3 py-1 font-semibold bg-primary/20 text-primary border-none rounded-lg">
              +{summary.selectedSensors.length - 7}
            </Badge>
          )}
        </div>
      </div>

    </div>
  );
};

export default AnalysisSummary;