import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '~/components/ui/select'
import { X, CalendarIcon, Upload } from 'lucide-react'
import { useContext, useState, useEffect, useCallback, useRef } from 'react' // + useEffect, useCallback, useRef
import { NavbarContext } from '.'
import { Button } from '~/components/ui/button'
import { Input } from '~/components/ui/input'
import { Label } from '~/components/ui/label'
import Spinner from '~/components/spinner'
import { useNavigation } from 'react-router'
import {
	ToggleGroup,
	ToggleGroupItem,
} from '~/components/ui/toggle-group'
import type maplibregl from 'maplibre-gl' // + type-only import, no runtime cost

interface CountryOption {
	name: string
	iso3: string // e.g. "DEU"
}
interface GeoJSONPosition extends Array<number> {}
interface GeoJSONGeometry {
	type: string
	coordinates: unknown
}
interface RegionFeature {
	type: 'Feature'
	geometry: GeoJSONGeometry
	properties: {
		shapeName: string
		shapeID: string
		shapeGroup: string
		shapeType: string
	}
}
interface RegionFeatureCollection {
	type: 'FeatureCollection'
	features: RegionFeature[]
}

interface ArchiveFilterState {
	sensorType: 'all' | 'indoor' | 'outdoor'

	areaType: 'shp' | 'kml' | 'geojson'
	areaFile?: File
	areaError?: string

	startDate: string
	endDate: string

	sensor: string
}

const emptyFilters: ArchiveFilterState = {
	sensorType: 'all',

	areaType: 'kml',
	areaFile: undefined,
	areaError: '',

	startDate: '',
	endDate: '',

	sensor: '',
}

export default function ArchiveFilterPanel() {
	const navigation = useNavigation()
	const { setOpen } = useContext(NavbarContext)

	const [filters, setFilters] =
		useState<ArchiveFilterState>(emptyFilters)

	const update = <K extends keyof ArchiveFilterState>(
		key: K,
		value: ArchiveFilterState[K],
	) => {
		setFilters((current) => ({
			...current,
			[key]: value,
		}))
	}

	const handleReset = () => {
		setFilters(emptyFilters)
	}

	const handleApply = () => {
		console.log(filters)
		setOpen(false)
	}

	return (
		<div className="relative py-2 dark:text-zinc-200">
			{navigation.state === 'loading' && (
				<div className="absolute inset-0 z-50 flex items-center justify-center bg-white/30 backdrop-blur-xs dark:bg-zinc-800/30">
					<Spinner />
				</div>
			)}

			<div className="flex max-h-[min(56vh,24rem)] flex-col gap-5 overflow-y-auto px-1 pb-2">

				{/* Sensor Type */}

				<div className="grid grid-cols-[10rem_1fr] items-center gap-4">

					<Label>
						Sensor type
					</Label>

					<ToggleGroup
						type="single"
						value={filters.sensorType}
						onValueChange={(value) => {
							if (!value) return

							update(
								'sensorType',
								value as 'all' | 'indoor' | 'outdoor',
							)
						}}
						className="justify-start gap-2"
					>
						<ToggleGroupItem
                            value="all"
                            className="data-[state=on]:bg-[#111827] data-[state=on]:text-white"
                        >All
						</ToggleGroupItem>

						<ToggleGroupItem
                            value="indoor"
                            className="data-[state=on]:bg-[#111827] data-[state=on]:text-white"
                        >
							Indoor
						</ToggleGroupItem>

						<ToggleGroupItem
                            value="outdoor"
                            className="data-[state=on]:bg-[#111827] data-[state=on]:text-white"
                        >
							Outdoor
						</ToggleGroupItem>
					</ToggleGroup>
				</div>

				{/* Area of Interest */}

				<div className="grid grid-cols-[10rem_1fr] items-center gap-4">

					<Label>
						Area of interest
					</Label>

					<div className="flex items-center gap-3">

	<Select
		value={filters.areaType}
		onValueChange={(value) =>
			update(
				'areaType',
				value as 'shp' | 'kml' | 'geojson',
			)
		}
	>
		<SelectTrigger className="w-44">
			<SelectValue placeholder="Select file type" />
		</SelectTrigger>

		<SelectContent>
			<SelectItem value="shp">SHP</SelectItem>
			<SelectItem value="kml">KML</SelectItem>
			<SelectItem value="geojson">GeoJSON</SelectItem>
		</SelectContent>
	</Select>

	<Button
		type="button"
		variant="outline"
	>
		<Upload className="mr-2 h-4 w-4" />
		Upload File
	</Button>

                    </div>
				</div>
                {/* Date Range */}

                <div className="grid grid-cols-[10rem_1fr] items-start gap-4">

                    <Label>
                        Date
                    </Label>

                    <div className="grid grid-cols-2 gap-3">

                        <div className="relative">
                            <CalendarIcon className="absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-gray-400" />

                            <Input
                                type="date"
                                value={filters.startDate}
                                onChange={(e) =>
                                    update('startDate', e.target.value)
                                }
                                className="pl-10"
                            />
                        </div>

                        <div className="relative">
                            <CalendarIcon className="absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-gray-400" />

                            <Input
                                type="date"
                                value={filters.endDate}
                                onChange={(e) =>
                                    update('endDate', e.target.value)
                                }
                                className="pl-10"
                            />
                        </div>

                    </div>
                </div>
				{/* Sensor */}

				<div className="grid grid-cols-[10rem_1fr] items-center gap-4">

					<Label>
						Type of sensor
					</Label>

					<Select
                        value={filters.sensor}
                        onValueChange={(value) => update('sensor', value)}
                    >
                        <SelectTrigger>
                            <SelectValue placeholder="Select a sensor type" />
                        </SelectTrigger>

                        <SelectContent>
                            <SelectItem value="temperature">
                                Temperature
                            </SelectItem>

                            <SelectItem value="humidity">
                                Humidity
                            </SelectItem>

                            <SelectItem value="pressure">
                                Pressure
                            </SelectItem>

                            <SelectItem value="pm25">
                                PM2.5
                            </SelectItem>

                            <SelectItem value="pm10">
                                PM10
                            </SelectItem>
                        </SelectContent>
                    </Select>

				</div>

			</div>

			<div className="mt-3 flex justify-end gap-2 border-t border-black/5 pt-3 dark:border-white/10">

				<Button
					variant="outline"
					className="h-8 rounded-md px-2 text-sm"
					onClick={handleReset}
				>
					<X className="mr-1 h-3.5 w-3.5" />
					Reset
				</Button>

				<Button
					className="h-8 rounded-md px-3 text-sm"
					onClick={handleApply}
				>
					Apply
				</Button>

			</div>

		</div>
	)
}