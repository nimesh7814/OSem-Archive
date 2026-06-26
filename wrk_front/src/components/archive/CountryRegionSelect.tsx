"use client";

import React, { useEffect, useState } from "react";
import { Search, Loader2 } from "lucide-react";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { apiUrl } from "@/lib/api";

interface CountryRegionSelectProps {
  country: string;
  region: string;
  onCountryChange: (c: string) => void;
  onRegionChange: (r: string) => void;
  className?: string;
}

export const CountryRegionSelect = ({
  country,
  region,
  onCountryChange,
  onRegionChange,
  className,
}: CountryRegionSelectProps) => {
  const [countries, setCountries] = useState<string[]>([]);
  const [regions, setRegions] = useState<string[]>([]);
  const [loadingCountries, setLoadingCountries] = useState(true);
  const [loadingRegions, setLoadingRegions] = useState(false);

  useEffect(() => {
    const fetchCountries = async () => {
      try {
        const res = await fetch(apiUrl("/regions"));
        if (res.ok) {
          const data = await res.json();
          setCountries((data.countries || []).map((item: { country: string }) => item.country));
        }
      } catch (err) {

        console.error("Failed to fetch countries", err);
      } finally {
        setLoadingCountries(false);
      }
    };
    fetchCountries();
  }, []);

  useEffect(() => {
    if (!country) {
      setRegions([]);
      return;
    }

    const fetchRegions = async () => {
      setLoadingRegions(true);
      try {
        const params = new URLSearchParams({ country });
        const res = await fetch(apiUrl("/regions", params));
        if (res.ok) {
          const data = await res.json();
          setRegions(data.regions || []);
        }
      } catch (err) {

        console.error("Failed to fetch regions", err);
      } finally {
        setLoadingRegions(false);
      }
    };

    fetchRegions();
  }, [country]);

  return (
    <div className={cn("grid grid-cols-1 sm:grid-cols-2 gap-6", className)}>
      <div className="space-y-3">
        <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-gray-400">
          Country
        </Label>
        <Select value={country} onValueChange={onCountryChange} disabled={loadingCountries}>
          <SelectTrigger className="h-14 w-full rounded-2xl border-none bg-[#F8F9FB] text-sm font-bold shadow-sm ring-1 ring-inset ring-gray-100 focus:ring-2 focus:ring-blue-500">
            <SelectValue placeholder={loadingCountries ? "Loading..." : "Select a country"} />
          </SelectTrigger>
          <SelectContent>
            {countries.map((c) => (
              <SelectItem key={c} value={c}>
                {c}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-3">
        <Label className="ml-1 text-[10px] font-black uppercase tracking-[0.2em] text-gray-400">
          Region
        </Label>
        <Select value={region} onValueChange={onRegionChange} disabled={!country || loadingRegions || regions.length === 0}>
          <SelectTrigger className="h-14 w-full rounded-2xl border-none bg-[#F8F9FB] text-sm font-bold shadow-sm ring-1 ring-inset ring-gray-100 focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-60">
            <SelectValue placeholder={
              !country ? "Select country first" 
              : loadingRegions ? "Loading..." 
              : regions.length === 0 ? "No regions found" 
              : "Select a region"
            } />
          </SelectTrigger>
          <SelectContent>
            {regions.map((r) => (
              <SelectItem key={r} value={r}>
                {r}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
};
