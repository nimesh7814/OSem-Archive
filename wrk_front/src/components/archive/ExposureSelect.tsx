"use client";

import React, { useEffect, useState } from "react";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { fetchExposures } from "@/lib/stationApi";
import { cn } from "@/lib/utils";

interface ExposureSelectProps {
  value: string;
  onChange: (value: string) => void;
  className?: string;
}

const ALL_EXPOSURES = "__all__";

const capitalize = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

export const ExposureSelect = ({ value, onChange, className }: ExposureSelectProps) => {
  const [exposures, setExposures] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchExposures()
      .then(setExposures)
      .finally(() => setLoading(false));
  }, []);

  return (
    <Select
      value={value || ALL_EXPOSURES}
      onValueChange={(next) => onChange(next === ALL_EXPOSURES ? "" : next)}
      disabled={loading}
    >
      <SelectTrigger className={cn("h-11 w-full rounded-xl", className)}>
        <SelectValue placeholder={loading ? "Loading…" : "All exposures"} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL_EXPOSURES}>All exposures</SelectItem>
        {exposures.map((exposure) => (
          <SelectItem key={exposure} value={exposure}>
            {capitalize(exposure)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
};

export default ExposureSelect;
