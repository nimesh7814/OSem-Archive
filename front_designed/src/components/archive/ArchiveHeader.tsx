"use client";

import React, { useEffect, useState } from 'react';
import { Radio, Database, Activity, Globe2 } from 'lucide-react';
import { fetchStats, type ArchiveStats } from '@/lib/stationApi';

const formatNumber = (num: number) => new Intl.NumberFormat('en-US').format(num);
const formatCompact = (num: number) =>
  new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(num);

type ArchiveHeaderProps = {
  stats?: ArchiveStats | null;
};

const ArchiveHeader = ({ stats: providedStats }: ArchiveHeaderProps) => {
  const [fetchedStats, setFetchedStats] = useState<ArchiveStats | null>(null);

  useEffect(() => {
    if (providedStats !== undefined) return;

    fetchStats()
      .then(setFetchedStats)
      .catch((error) => console.error('Failed to fetch stats', error));
  }, [providedStats]);

  const stats = providedStats !== undefined ? providedStats : fetchedStats;

  const items = [
    { label: 'Stations', value: stats ? formatNumber(stats.total_stations) : '—', icon: Radio },
    { label: 'Sensors', value: stats ? formatNumber(stats.total_sensors) : '—', icon: Database },
    { label: 'Readings', value: stats ? formatCompact(stats.total_readings) : '—', icon: Activity },
    { label: 'Countries', value: stats ? formatNumber(stats.total_countries) : '—', icon: Globe2 },
  ];

  return (
    <header className="h-16 bg-white/70 border-border backdrop-blur-xl border-b sticky top-0 z-40 px-6 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <img src="/assets/logo.svg" alt="OSeM Archive logo" className="h-7 w-7 shrink-0" />
        <h1 className="text-lg font-semibold tracking-tight text-foreground">OSeM Archive</h1>
      </div>

      <div className="flex items-center gap-4">
        {items.map((item) => (
          <div key={item.label} className="flex items-center gap-1.5">
            <item.icon className="h-3.5 w-3.5 text-primary" />
            <span className="text-sm font-semibold text-foreground">{item.value}</span>
            <span className="text-xs font-medium text-muted-foreground">{item.label}</span>
          </div>
        ))}
      </div>
    </header>
  );
};

export default ArchiveHeader;
