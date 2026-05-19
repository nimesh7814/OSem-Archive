"use client";

import React, { useEffect, useState } from 'react';
import { Radio, Globe2, Database, Activity } from 'lucide-react';
import { motion } from 'framer-motion';
import { apiUrl } from '@/lib/api';

type SummaryData = {
  total_stations: number;
  total_sensors: number;
  total_readings: number;
  total_countries: number;
};

const formatNumber = (num: number) => new Intl.NumberFormat('en-US').format(num);
const formatCompact = (num: number) => new Intl.NumberFormat('en-US', { notation: "compact", maximumFractionDigits: 1 }).format(num);

const StatsSection = () => {
  const [data, setData] = useState<SummaryData | null>(null);

  useEffect(() => {
    const fetchSummary = async () => {
      try {
        const response = await fetch(apiUrl('/summary'));
        if (response.ok) {
          const json = await response.json();
          setData(json);
        }
      } catch (error) {

        console.error("Failed to fetch summary data:", error);
      }
    };

    fetchSummary();
  }, []);

  const stats = [
    { label: 'Total Stations', value: data ? formatNumber(data.total_stations) : '-', icon: Radio, color: 'text-blue-600' },
    { label: 'Sensors', value: data ? formatNumber(data.total_sensors) : '-', icon: Database, color: 'text-emerald-600' },
    { label: 'Total Readings', value: data ? formatCompact(data.total_readings) : '-', icon: Activity, color: 'text-rose-600' },
    { label: 'Countries', value: data ? formatNumber(data.total_countries) : '-', icon: Globe2, color: 'text-indigo-600' },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 w-full max-w-5xl mx-auto">
      {stats.map((stat, index) => (
        <motion.div
          key={stat.label}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: index * 0.1 }}
          className="bg-white/60 backdrop-blur-md p-6 rounded-[24px] border border-white/20 shadow-sm hover:shadow-md transition-all"
        >
          <stat.icon className={`w-6 h-6 ${stat.color} mb-3`} />
          <div className="text-2xl font-bold text-gray-900">{stat.value}</div>
          <div className="text-sm text-gray-500 font-medium">{stat.label}</div>
        </motion.div>
      ))}
    </div>
  );
};

export default StatsSection;
