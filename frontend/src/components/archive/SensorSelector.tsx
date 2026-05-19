"use client";

import React, { useEffect, useState } from 'react';
import { Check, ChevronsUpDown } from 'lucide-react';
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from '@/lib/utils';
import { apiUrl } from '@/lib/api';

// Keeping a fallback list just in case, but we will fetch from API

export const FALLBACK_SENSOR_TYPES = [
  { value: 'pm25', label: 'PM 2.5' },
  { value: 'pm10', label: 'PM 10' },
  { value: 'temp', label: 'Temperature' },
];

interface SensorSelectorProps {
  selected: string[];
  onToggle: (value: string | string[]) => void;
  className?: string;
}

const formatSensorLabel = (val: string) => {
  return val.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
};

const SensorSelector = ({ selected, onToggle, className }: SensorSelectorProps) => {
  const [sensors, setSensors] = useState<{value: string, label: string}[]>([]);
  const [loading, setLoading] = useState(true);

  // Expose the total count or values back up to parent when they click "All Selected"
  const handleToggleAll = () => {
    if (selected.length === sensors.length) {
      onToggle([]); // Clear all
    } else {
      onToggle(sensors.map(s => s.value)); // Select all
    }
  };

  useEffect(() => {
    const fetchSensors = async () => {
      try {
        const res = await fetch(apiUrl('/sensor_categories'));
        if (res.ok) {
          const data = await res.json();
          if (data.categories && Array.isArray(data.categories)) {
            setSensors(data.categories.map((c: string) => ({
              value: c,
              label: formatSensorLabel(c)
            })));
          }
        }
      } catch (err) {

        console.error("Failed to fetch sensors", err);
        setSensors(FALLBACK_SENSOR_TYPES);
      } finally {
        setLoading(false);
      }
    };
    fetchSensors();
  }, []);

  const allSelected = sensors.length > 0 && selected.length === sensors.length;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" className={cn("h-11 rounded-xl bg-gray-50 border-none text-sm font-bold justify-between hover:bg-gray-100", className)} disabled={loading}>
          {loading ? "Loading..." : allSelected ? "All Selected" : `${selected.length} Selected`}
          <ChevronsUpDown className="ml-2 h-4 w-4 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-2 max-h-80 overflow-y-auto rounded-2xl shadow-xl border-none">
        <div className="space-y-1">
          <div
            className={cn(
              "flex items-center px-3 py-2 rounded-lg cursor-pointer text-sm font-bold transition-colors",
              allSelected ? "bg-blue-50 text-blue-700" : "text-gray-700 hover:bg-gray-100"
            )}
            onClick={handleToggleAll}
          >
            <div className={cn(
              "mr-3 flex h-4 w-4 items-center justify-center rounded border transition-all",
              allSelected ? "bg-blue-600 border-blue-600 text-white" : "border-gray-300"
            )}>
              {allSelected && <Check className="h-3 w-3" />}
            </div>
            All Selected
          </div>
          {sensors.map((sensor) => (
            <div
              key={sensor.value}
              className={cn(
                "flex items-center px-3 py-2 rounded-lg cursor-pointer text-sm font-bold transition-colors",
                selected.includes(sensor.value) ? "bg-blue-50 text-blue-700" : "text-gray-700 hover:bg-gray-100"
              )}
              onClick={() => onToggle(sensor.value)}
            >
              <div className={cn(
                "mr-3 flex h-4 w-4 items-center justify-center rounded border transition-all",
                selected.includes(sensor.value) ? "bg-blue-600 border-blue-600 text-white" : "border-gray-300"
              )}>
                {selected.includes(sensor.value) && <Check className="h-3 w-3" />}
              </div>
              {sensor.label}
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
};

export default SensorSelector;