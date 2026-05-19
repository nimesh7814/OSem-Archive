"use client";

import { ArrowRight, Table, Map } from "lucide-react";
import { useNavigate } from "react-router-dom";
import StatsSection from "@/components/StatsSection";
import { motion } from "framer-motion";

const Index = () => {
  const navigate = useNavigate();

  return (
    <div className="min-h-screen flex flex-col bg-[#F5F5F7] overflow-x-hidden font-sans antialiased">
      <main className="flex-1 flex flex-col items-center justify-center px-6 py-20">
        <motion.div 
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="max-w-4xl w-full text-center space-y-12"
        >
          <div className="space-y-6">
            <motion.div
              initial={{ scale: 0.8 }}
              animate={{ scale: 1 }}
              className="inline-flex items-center justify-center p-4 bg-white rounded-[32px] shadow-xl shadow-blue-100/50 mb-4"
            >
              <img src="/assets/logo.svg" alt="OSeM Archive logo" className="h-16 w-16" />
            </motion.div>
            <h1 className="text-6xl md:text-7xl font-extrabold tracking-tight text-gray-900 leading-[1.1]">
              OSeM Archive
            </h1>
            <p className="text-xl md:text-2xl text-gray-600 max-w-2xl mx-auto leading-relaxed font-medium tracking-tight">
              The definitive portal for environmental data retrieval. <br className="hidden md:block" />
              Simple, powerful, and precise.
            </p>
          </div>

          <StatsSection />

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 pt-8">
            <motion.div
              whileHover={{ y: -5 }}
              className="bg-white p-8 rounded-[40px] shadow-sm border border-gray-100 text-left group cursor-pointer"
              onClick={() => navigate('/archive?mode=bulk')}
            >
              <div className="h-14 w-14 bg-blue-50 rounded-2xl flex items-center justify-center mb-6 group-hover:bg-blue-600 transition-colors">
                <Table className="w-7 h-7 text-blue-600 group-hover:text-white transition-colors" />
              </div>
              <h3 className="text-2xl font-extrabold text-gray-900 mb-2 tracking-tight">Country / Region</h3>
              <p className="text-gray-600 mb-6 font-medium leading-relaxed">Download full datasets by country or region in one click.</p>
              <div className="flex items-center text-blue-600 font-bold tracking-tight">
                Get Started <ArrowRight className="ml-2 w-4 h-4 group-hover:translate-x-1 transition-transform" />
              </div>
            </motion.div>

            <motion.div
              whileHover={{ y: -5 }}
              className="bg-white p-8 rounded-[40px] shadow-sm border border-gray-100 text-left group cursor-pointer"
              onClick={() => navigate('/archive?mode=custom')}
            >
              <div className="h-14 w-14 bg-indigo-50 rounded-2xl flex items-center justify-center mb-6 group-hover:bg-indigo-600 transition-colors">
                <Map className="w-7 h-7 text-indigo-600 group-hover:text-white transition-colors" />
              </div>
              <h3 className="text-2xl font-extrabold text-gray-900 mb-2 tracking-tight">Custom</h3>
              <p className="text-gray-600 mb-6 font-medium leading-relaxed">Use our interactive map to select specific stations and sensors.</p>
              <div className="flex items-center text-indigo-600 font-bold tracking-tight">
                Explore Map <ArrowRight className="ml-2 w-4 h-4 group-hover:translate-x-1 transition-transform" />
              </div>
            </motion.div>
          </div>
        </motion.div>
      </main>
      
      <footer className="py-12 border-t border-gray-100 bg-white/50">
        <div className="max-w-7xl mx-auto px-6 flex flex-col md:flex-row items-center justify-between gap-6">
          <div className="flex items-center gap-3">
            <img src="/assets/logo.svg" alt="OSeM Archive logo" className="h-6 w-6" />
            <span className="font-extrabold text-gray-900 tracking-tight">OSeM Archive</span>
          </div>
          <p className="text-sm text-gray-500 font-medium tracking-tight">© {new Date().getFullYear()} Environmental Data Portal. All rights reserved.</p>
        </div>
      </footer>
    </div>
  );
};

export default Index;