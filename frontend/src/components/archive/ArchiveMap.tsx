"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  MapContainer,
  TileLayer,
  useMap,
  useMapEvents,
  Polygon,
  CircleMarker,
  Tooltip,
} from "react-leaflet";
import type { Map as LeafletMap } from "leaflet";
import "leaflet/dist/leaflet.css";

import { motion, AnimatePresence } from "framer-motion";
import {
  X,
  Info,
  ExternalLink,
  Square,
  Hexagon,
  Hand,
  Eraser,
  Check,
  Loader2,
  Plus,
  Minus,
  LocateFixed,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  fetchStationInfo,
  type MapStation,
  type StationInfo,
  type GeoPoint,
} from "@/lib/stationApi";

type DrawingMode = "none" | "rectangle" | "polygon";

export type AOIShape = {
  type: "Feature";
  properties: {
    source: "rectangle" | "polygon";
  };
  geometry: {
    type: "Polygon";
    coordinates: GeoPoint[][];
  };
};

interface ArchiveMapProps {
  center?: GeoPoint;
  zoom?: number;
  fullBleed?: boolean;
  isSearching?: boolean;
  hasResults?: boolean;
  hasRunSearch?: boolean;
  drawingMode?: DrawingMode;
  aoi?: AOIShape | null;
  focusAoiToken?: number;
  stations: MapStation[];
  isLoadingStations?: boolean;
  onDrawingModeChange?: (mode: DrawingMode) => void;
  onClear?: () => void;
  onAOIChange?: (aoi: AOIShape | null) => void;
}

const createRectangleAOI = (start: GeoPoint, end: GeoPoint): AOIShape => {
  const west = Math.min(start[0], end[0]);
  const east = Math.max(start[0], end[0]);
  const south = Math.min(start[1], end[1]);
  const north = Math.max(start[1], end[1]);
  return {
    type: "Feature",
    properties: { source: "rectangle" },
    geometry: {
      type: "Polygon",
      coordinates: [
        [
          [west, north],
          [east, north],
          [east, south],
          [west, south],
          [west, north],
        ],
      ],
    },
  };
};

const createPolygonAOI = (points: GeoPoint[]): AOIShape | null => {
  if (points.length < 3) return null;
  return {
    type: "Feature",
    properties: { source: "polygon" },
    geometry: {
      type: "Polygon",
      coordinates: [[...points, points[0]]],
    },
  };
};

// AOI coordinates are [lng, lat]. Convert to leaflet [lat, lng].
const toLatLngs = (ring: GeoPoint[]): [number, number][] =>
  ring.map(([lng, lat]) => [lat, lng]);

const MapController: React.FC<{ center: GeoPoint; zoom: number }> = ({
  center,
  zoom,
}) => {
  const map = useMap();
  const lastRef = useRef<{ c: GeoPoint; z: number } | null>(null);
  useEffect(() => {
    const last = lastRef.current;
    if (
      !last ||
      last.c[0] !== center[0] ||
      last.c[1] !== center[1] ||
      last.z !== zoom
    ) {
      map.flyTo([center[1], center[0]], zoom, { duration: 0.6 });
      lastRef.current = { c: center, z: zoom };
    }
  }, [center, zoom, map]);
  return null;
};

const AoiFocusController: React.FC<{
  aoi: AOIShape | null;
  focusAoiToken?: number;
}> = ({ aoi, focusAoiToken }) => {
  const map = useMap();
  const lastFocusTokenRef = useRef<number | null>(null);

  useEffect(() => {
    if (
      !aoi ||
      focusAoiToken == null ||
      lastFocusTokenRef.current === focusAoiToken
    )
      return;

    const positions = toLatLngs(aoi.geometry.coordinates[0]);
    if (positions.length > 0) {
      map.fitBounds(positions, { padding: [40, 40], maxZoom: 13 });
      lastFocusTokenRef.current = focusAoiToken;
    }
  }, [aoi, focusAoiToken, map]);

  return null;
};

const DrawingLayer: React.FC<{
  drawingMode: DrawingMode;
  onAOIChange?: (aoi: AOIShape | null) => void;
  onDrawingModeChange?: (mode: DrawingMode) => void;
  polygonPoints: GeoPoint[];
  setPolygonPoints: React.Dispatch<React.SetStateAction<GeoPoint[]>>;
  rectStart: GeoPoint | null;
  setRectStart: React.Dispatch<React.SetStateAction<GeoPoint | null>>;
  rectEnd: GeoPoint | null;
  setRectEnd: React.Dispatch<React.SetStateAction<GeoPoint | null>>;
}> = ({
  drawingMode,
  onAOIChange,
  onDrawingModeChange,
  polygonPoints,
  setPolygonPoints,
  rectStart,
  setRectStart,
  rectEnd,
  setRectEnd,
}) => {
  const map = useMap();

  // Disable map dragging while drawing a rectangle (after first click)
  useEffect(() => {
    if (drawingMode === "rectangle" && rectStart) {
      map.dragging.disable();
    } else {
      map.dragging.enable();
    }
    return () => {
      map.dragging.enable();
    };
  }, [drawingMode, rectStart, map]);

  useMapEvents({
    click(e) {
      if (drawingMode === "rectangle") {
        const pt: GeoPoint = [e.latlng.lng, e.latlng.lat];
        if (!rectStart) {
          setRectStart(pt);
          setRectEnd(pt);
        } else {
          const aoi = createRectangleAOI(rectStart, pt);
          onAOIChange?.(aoi);
          setRectStart(null);
          setRectEnd(null);
          onDrawingModeChange?.("none");
        }
      } else if (drawingMode === "polygon") {
        const pt: GeoPoint = [e.latlng.lng, e.latlng.lat];
        setPolygonPoints((prev) => [...prev, pt]);
      }
    },
    mousemove(e) {
      if (drawingMode === "rectangle" && rectStart) {
        setRectEnd([e.latlng.lng, e.latlng.lat]);
      }
    },
  });

  return null;
};

const ArchiveMap = ({
  center = [0, 20],
  zoom = 2,
  fullBleed = false,
  isSearching = false,
  drawingMode = "none",
  aoi = null,
  focusAoiToken,
  stations,
  isLoadingStations = false,
  onDrawingModeChange,
  onClear,
  onAOIChange,
}: ArchiveMapProps) => {
  const navigate = useNavigate();
  const [selectedStation, setSelectedStation] = useState<MapStation | null>(
    null,
  );
  const [selectedStationInfo, setSelectedStationInfo] =
    useState<StationInfo | null>(null);
  const [stationInfoLoading, setStationInfoLoading] = useState(false);

  const [polygonPoints, setPolygonPoints] = useState<GeoPoint[]>([]);
  const [rectStart, setRectStart] = useState<GeoPoint | null>(null);
  const [rectEnd, setRectEnd] = useState<GeoPoint | null>(null);

  const mapRef = useRef<LeafletMap | null>(null);
  const [isLocating, setIsLocating] = useState(false);

  // Reset preview state when switching drawing modes
  useEffect(() => {
    setPolygonPoints([]);
    setRectStart(null);
    setRectEnd(null);
  }, [drawingMode]);

  useEffect(() => {
    if (!selectedStation) {
      setSelectedStationInfo(null);
      setStationInfoLoading(false);
    }
  }, [selectedStation]);

  // Active AOI polygon (already-set selection)
  const activeAOIPositions = useMemo<[number, number][] | null>(() => {
    if (!aoi) return null;
    return toLatLngs(aoi.geometry.coordinates[0]);
  }, [aoi]);

  // Preview AOI while drawing
  const previewAOIPositions = useMemo<[number, number][] | null>(() => {
    if (drawingMode === "rectangle" && rectStart && rectEnd) {
      const preview = createRectangleAOI(rectStart, rectEnd);
      return toLatLngs(preview.geometry.coordinates[0]);
    }
    if (drawingMode === "polygon" && polygonPoints.length >= 2) {
      return polygonPoints.map(([lng, lat]) => [lat, lng] as [number, number]);
    }
    return null;
  }, [drawingMode, rectStart, rectEnd, polygonPoints]);

  const handleToolChange = (mode: DrawingMode) => {
    onDrawingModeChange?.(mode);
  };

  const handleCancelDrawing = () => {
    setRectStart(null);
    setRectEnd(null);
    setPolygonPoints([]);
    onDrawingModeChange?.("none");
  };

  const clearDrawing = () => {
    setRectStart(null);
    setRectEnd(null);
    setPolygonPoints([]);
    setSelectedStation(null);
    onAOIChange?.(null);
    onDrawingModeChange?.("none");
    onClear?.();
  };

  const finishPolygon = (event: React.MouseEvent) => {
    event.stopPropagation();
    const nextAOI = createPolygonAOI(polygonPoints);
    if (!nextAOI) return;
    onAOIChange?.(nextAOI);
    onDrawingModeChange?.("none");
    setPolygonPoints([]);
  };

  const activeAOIStroke =
    aoi?.properties.source === "rectangle" ? "#3b82f6" : "#10b981";
  const activeAOIFill =
    aoi?.properties.source === "rectangle" ? "#3b82f6" : "#10b981";
  const previewAOIStroke = drawingMode === "rectangle" ? "#3b82f6" : "#10b981";
  const previewAOIFill = drawingMode === "rectangle" ? "#3b82f6" : "#10b981";

  const zoomIn = () => mapRef.current?.zoomIn();
  const zoomOut = () => mapRef.current?.zoomOut();

  const goToCurrentLocation = () => {
    if (!navigator.geolocation || isLocating) return;

    setIsLocating(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        mapRef.current?.flyTo(
          [position.coords.latitude, position.coords.longitude],
          13,
          { duration: 0.7 },
        );
        setIsLocating(false);
      },
      () => {
        setIsLocating(false);
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  };

  return (
    <div
      className={`w-full h-full overflow-hidden relative ${fullBleed ? "" : "rounded-[32px] border border-white shadow-inner"}`}
    >
      {/* Drawing toolbar */}
      <div className="absolute top-6 left-6 z-[600] flex items-start gap-2">
        <div className="bg-white/95 backdrop-blur-md rounded-xl border border-slate-200 shadow-lg overflow-hidden">
          <div className="flex flex-col">
            <Button
              variant="ghost"
              size="icon"
              title="Draw rectangle"
              className={`h-9 w-9 rounded-none border-b border-slate-200 ${drawingMode === "rectangle" ? "bg-slate-900 text-white hover:bg-slate-800 hover:text-white" : "bg-white text-slate-700 hover:bg-slate-100"}`}
              onClick={() =>
                handleToolChange(
                  drawingMode === "rectangle" ? "none" : "rectangle",
                )
              }
            >
              <Square className="w-4 h-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Draw polygon"
              className={`h-9 w-9 rounded-none border-b border-slate-200 ${drawingMode === "polygon" ? "bg-slate-900 text-white hover:bg-slate-800 hover:text-white" : "bg-white text-slate-700 hover:bg-slate-100"}`}
              onClick={() =>
                handleToolChange(drawingMode === "polygon" ? "none" : "polygon")
              }
            >
              <Hexagon className="w-4 h-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Pan"
              className={`h-9 w-9 rounded-none border-b border-slate-200 ${drawingMode === "none" ? "bg-slate-900 text-white hover:bg-slate-800 hover:text-white" : "bg-white text-slate-700 hover:bg-slate-100"}`}
              onClick={() => handleToolChange("none")}
            >
              <Hand className="w-4 h-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              title="Clear selection"
              className="h-9 w-9 rounded-none bg-white text-slate-700 hover:bg-red-50 hover:text-red-600"
              onClick={clearDrawing}
            >
              <Eraser className="w-4 h-4" />
            </Button>
          </div>
        </div>

        {drawingMode !== "none" && (
          <div className="flex flex-col gap-1">
            <Button
              variant="secondary"
              size="icon"
              className="h-9 w-9 rounded-md bg-white border border-slate-300 text-red-500 hover:bg-red-50 hover:text-red-600 shadow-lg"
              onClick={handleCancelDrawing}
              title="Cancel drawing"
            >
              <X className="w-4 h-4" />
            </Button>

            {drawingMode === "polygon" && polygonPoints.length > 2 && (
              <Button
                variant="secondary"
                size="icon"
                className="h-9 w-9 rounded-md bg-white border border-slate-300 text-emerald-600 hover:bg-emerald-50 hover:text-emerald-700 shadow-lg"
                onClick={finishPolygon}
                title="Finish polygon"
              >
                <Check className="w-4 h-4" />
              </Button>
            )}
          </div>
        )}
      </div>

      {/* Zoom controls (custom, replace default which appears top-left) */}
      <div className="absolute top-6 right-6 z-[600] flex flex-col bg-white/95 rounded-xl border border-slate-200 shadow-lg overflow-hidden">
        <Button
          variant="ghost"
          size="icon"
          onClick={zoomIn}
          className="h-9 w-9 rounded-none border-b border-slate-200 text-slate-700 hover:bg-slate-100"
          title="Zoom in"
        >
          <Plus className="w-4 h-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          onClick={zoomOut}
          className="h-9 w-9 rounded-none text-slate-700 hover:bg-slate-100"
          title="Zoom out"
        >
          <Minus className="w-4 h-4" />
        </Button>
      </div>

      {/* Hint banner while drawing */}
      {drawingMode !== "none" && (
        <div className="absolute top-6 left-1/2 -translate-x-1/2 z-[600] bg-white text-slate-700 px-4 py-2 rounded-md text-xs shadow-lg border border-slate-200 font-medium">
          {drawingMode === "rectangle"
            ? rectStart
              ? "Click to place opposite corner"
              : "Click to place first corner"
            : polygonPoints.length > 2
              ? "Add more points or click the check to finish"
              : polygonPoints.length > 0
                ? "Click to add another vertex"
                : "Click to add the first vertex"}
        </div>
      )}

      {/* Loading overlays */}
      <AnimatePresence>
        {isLoadingStations && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 z-[550] flex items-center justify-center bg-white/40 backdrop-blur-sm pointer-events-none"
          >
            <div className="flex items-center gap-3 rounded-2xl bg-white px-5 py-3 shadow-xl border border-slate-200">
              <Loader2 className="w-5 h-5 animate-spin text-blue-600" />
              <span className="text-sm font-bold text-slate-700">
                Loading stations…
              </span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {isSearching && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute top-20 left-1/2 -translate-x-1/2 z-[600] flex items-center gap-3 rounded-full bg-white/95 px-4 py-2 shadow-lg border border-slate-200 pointer-events-none"
          >
            <Loader2 className="w-4 h-4 animate-spin text-blue-600" />
            <span className="text-xs font-bold text-slate-700">
              Running query…
            </span>
          </motion.div>
        )}
      </AnimatePresence>

      <MapContainer
        center={[center[1], center[0]]}
        zoom={zoom}
        minZoom={2}
        maxZoom={18}
        worldCopyJump
        zoomControl={false}
        preferCanvas
        className="w-full h-full"
        ref={(instance) => {
          mapRef.current = instance;
        }}
        style={{
          background: "#dfeaf4",
          cursor: drawingMode !== "none" ? "crosshair" : "grab",
        }}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
          subdomains={["a", "b", "c", "d"]}
          updateWhenIdle={false}
          updateWhenZooming={false}
          keepBuffer={4}
        />

        <MapController center={center} zoom={zoom} />
        <AoiFocusController aoi={aoi} focusAoiToken={focusAoiToken} />

        <DrawingLayer
          drawingMode={drawingMode}
          onAOIChange={onAOIChange}
          onDrawingModeChange={onDrawingModeChange}
          polygonPoints={polygonPoints}
          setPolygonPoints={setPolygonPoints}
          rectStart={rectStart}
          setRectStart={setRectStart}
          rectEnd={rectEnd}
          setRectEnd={setRectEnd}
        />

        {activeAOIPositions && (
          <Polygon
            positions={activeAOIPositions}
            pathOptions={{
              color: activeAOIStroke,
              fillColor: activeAOIFill,
              fillOpacity: 0.18,
              weight: 2,
              dashArray: "6 4",
            }}
          />
        )}

        {previewAOIPositions && (
          <Polygon
            positions={previewAOIPositions}
            pathOptions={{
              color: previewAOIStroke,
              fillColor: previewAOIFill,
              fillOpacity: 0.12,
              weight: 2,
              dashArray: "4 4",
            }}
          />
        )}

        {drawingMode === "polygon" &&
          polygonPoints.map((point, index) => (
            <CircleMarker
              key={`${point[0]}-${point[1]}-${index}`}
              center={[point[1], point[0]]}
              radius={5}
              pathOptions={{
                color: "#ffffff",
                weight: 2,
                fillColor: "#10b981",
                fillOpacity: 1,
              }}
            />
          ))}

        {stations.map((station) => (
          <CircleMarker
            key={station.id}
            center={[station.coordinates[1], station.coordinates[0]]}
            radius={5}
            pathOptions={{
              color: "#ffffff",
              weight: 1.5,
              fillColor: "#2563eb",
              fillOpacity: 1,
            }}
            eventHandlers={{
              click: async () => {
                if (drawingMode !== "none") return;
                setSelectedStation(station);
                setSelectedStationInfo(null);
                setStationInfoLoading(true);
                const info = await fetchStationInfo(station.id);
                setSelectedStationInfo(info);
                setStationInfoLoading(false);
              },
            }}
          >
            <Tooltip direction="top" offset={[0, -6]} opacity={0.95} sticky>
              <span className="text-xs font-bold">
                {station.name ?? station.id}
              </span>
            </Tooltip>
          </CircleMarker>
        ))}
      </MapContainer>

      <div className="absolute bottom-6 right-6 z-[600] flex flex-col gap-2">
        <Button
          variant="ghost"
          size="icon"
          onClick={goToCurrentLocation}
          disabled={isLocating}
          className="h-10 w-10 rounded-xl bg-white/95 border border-slate-200 shadow-lg text-slate-700 hover:bg-slate-100"
          title="Go to current location"
        >
          <LocateFixed
            className={`h-4 w-4 ${isLocating ? "animate-pulse" : ""}`}
          />
        </Button>
      </div>

      <AnimatePresence>
        {selectedStation && (
          <motion.div
            initial={{ opacity: 0, scale: 0.94, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.94, y: 20 }}
            className="absolute bottom-10 left-1/2 -translate-x-1/2 w-80 bg-white rounded-[28px] shadow-2xl border border-slate-300 p-6 z-[700]"
          >
            <Button
              variant="ghost"
              size="icon"
              className="absolute top-4 right-4 rounded-full h-8 w-8"
              onClick={() => setSelectedStation(null)}
            >
              <X className="w-4 h-4" />
            </Button>

            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <div className="h-12 w-12 bg-blue-50 rounded-2xl flex items-center justify-center">
                  <Info className="w-6 h-6 text-blue-600" />
                </div>
                <div>
                  <h4 className="font-black text-slate-900 tracking-tight">
                    {selectedStationInfo?.name ?? selectedStation.id}
                  </h4>
                  <p className="text-xs font-bold text-slate-500 uppercase tracking-widest">
                    Station ID: {selectedStation.id}
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4 py-4 border-y border-slate-100">
                <div>
                  <p className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">
                    Sensors
                  </p>
                  <p className="text-lg font-black text-slate-900">
                    {stationInfoLoading
                      ? "…"
                      : (selectedStationInfo?.total_sensors ??
                        selectedStation.sensors ??
                        "—")}
                  </p>
                </div>
                <div>
                  <p className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-1">
                    Readings
                  </p>
                  <p className="text-lg font-black text-slate-900">
                    {stationInfoLoading
                      ? "…"
                      : (selectedStationInfo?.total_readings?.toLocaleString() ??
                        selectedStation.readings ??
                        "—")}
                  </p>
                </div>
              </div>

              <div>
                <p className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-2">
                  Sensor Types
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {(selectedStationInfo?.categories
                    ? selectedStationInfo.categories
                        .split(",")
                        .map((item) => item.trim())
                        .filter(Boolean)
                    : (selectedStation.types ?? [])
                  )
                    .slice(0, 4)
                    .map((type) => (
                      <span
                        key={type}
                        className="px-2 py-1 bg-slate-100 rounded-md text-[10px] font-bold text-slate-600"
                      >
                        {type}
                      </span>
                    ))}
                  {(selectedStationInfo?.categories
                    ? selectedStationInfo.categories
                        .split(",")
                        .map((item) => item.trim())
                        .filter(Boolean)
                    : (selectedStation.types ?? [])
                  ).length > 4 && (
                    <span className="px-2 py-1 bg-blue-50 rounded-md text-[10px] font-black text-blue-700 border border-blue-200">
                      +
                      {(selectedStationInfo?.categories
                        ? selectedStationInfo.categories
                            .split(",")
                            .map((item) => item.trim())
                            .filter(Boolean)
                        : (selectedStation.types ?? [])
                      ).length - 4}
                    </span>
                  )}
                </div>
              </div>

              <Button
                className="w-full rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold h-11"
                onClick={() => {
                  navigate(`/archive/station/${selectedStation.id}`);
                  setSelectedStation(null);
                }}
              >
                View More <ExternalLink className="w-4 h-4 ml-2" />
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
};

export default ArchiveMap;
