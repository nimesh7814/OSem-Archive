import { Popover, PopoverContent, PopoverTrigger } from "~/components/ui/popover"
import {
    Command,
    CommandEmpty,
    CommandGroup,
    CommandInput,
    CommandItem,
} from "~/components/ui/command"
import { Check, ChevronsUpDown } from "lucide-react"
import type { Feature } from "geojson"
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '~/components/ui/select'
import { X, CalendarIcon, Upload } from 'lucide-react'
import { useContext, useState, useEffect, useRef } from 'react'
import { NavbarContext } from '.'
import { Button } from '~/components/ui/button'
import { Input } from '~/components/ui/input'
import { Label } from '~/components/ui/label'
import Spinner from '~/components/spinner'
import { archiveApiUrl, validateAOI } from '~/lib/archive-api'
import { useNavigation } from 'react-router'
import {
	ToggleGroup,
	ToggleGroupItem,
} from '~/components/ui/toggle-group'
export interface ArchiveFilterState {
    sensorType: 'all' | 'indoor' | 'outdoor'

    country?: string
    region?: string

    areaFile?: File
    areaGeoJSON?: Feature
    areaError?: string

    startDate: string
    endDate: string

    sensors: string[]
}

const MIN_DATE = '2014-06-03'

const formatDate = (date: Date) => {
	return date.toISOString().split('T')[0]
}

const addYears = (dateString: string, years: number) => {
	const date = new Date(dateString)
	date.setFullYear(date.getFullYear() + years)
	return formatDate(date)
}

interface ArchiveFilterPanelProps {
    onZoomToAOI: (feature: Feature) => void
    onApply: (filters: ArchiveFilterState) => void
    disabled?: boolean
    filters: ArchiveFilterState
    onFiltersChange: (updater: ArchiveFilterState | ((prev: ArchiveFilterState) => ArchiveFilterState)) => void
    selectedCountry: string
    onSelectedCountryChange: (value: string) => void
    selectedRegion: string
    onSelectedRegionChange: (value: string) => void
}

export const emptyFilters: ArchiveFilterState = {
    sensorType: 'all',

    country: '',
    region: '',

    areaFile: undefined,
    areaError: '',

    startDate: MIN_DATE,
    endDate: '',

    sensors: [],
}

function FilterChip({
	label,
	onClear,
}: {
	label: string
	onClear?: () => void
}) {
	return (
		<span className="inline-flex items-center gap-1.5 rounded-full border border-black/10 bg-white px-3 py-1 text-xs text-gray-700 dark:border-white/10 dark:bg-zinc-800 dark:text-zinc-200">
			{label}
			{onClear && (
				<button
					type="button"
					onClick={onClear}
					className="text-gray-400 hover:text-gray-600"
				>
					<X className="h-3 w-3" />
				</button>
			)}
		</span>
	)
}

export default function ArchiveFilterPanel({
	onZoomToAOI,
	onApply,
	disabled,
	filters,
	onFiltersChange,
	selectedCountry,
	onSelectedCountryChange,
	selectedRegion,
	onSelectedRegionChange,
}: ArchiveFilterPanelProps) {
	const navigation = useNavigation()
	const { setOpen } = useContext(NavbarContext)

	const fileInputRef = useRef<HTMLInputElement>(null)
	const [dragging, setDragging] = useState(false)
	const [countries, setCountries] = useState<any[]>([])
	const [sensorOpen, setSensorOpen] = useState(false)
	const [phenomena, setPhenomena] = useState<string[]>([])

	useEffect(() => {
		const loadCountries = async () => {
			try {
				const response = await fetch(archiveApiUrl('/countries'))
				if (!response.ok) throw new Error(await response.text())
				const data = await response.json()
				setCountries(data.countries ?? [])

				const p = await fetch(archiveApiUrl('/phenomena'))
				if (!p.ok) throw new Error(await p.text())
				const pdata = await p.json()
				setPhenomena(pdata.phenomena ?? [])
			} catch (err) {
				console.error('Failed to load archive filters:', err)
			}
		}

		void loadCountries()
	}, [])

	const update = <K extends keyof ArchiveFilterState>(
		key: K,
		value: ArchiveFilterState[K],
	) => {
		onFiltersChange((current) => ({
			...current,
			[key]: value,
		}))
	}

	const handleAOIFile = async (file: File) => {
	const name = file.name.toLowerCase()

		if (
			!name.endsWith('.shp') &&
			!name.endsWith('.kml') &&
			!name.endsWith('.geojson') &&
			!name.endsWith('.json')
		) {
			update(
				'areaError',
				'Only SHP, KML and GeoJSON files are allowed.',
			)

			update('areaFile', undefined)
			return
		}

		try {
			const result = await validateAOI(file)

			update('areaFile', file)
			update('areaGeoJSON', result)
			update('areaError', '')
			if (result?.geometry) {
				onZoomToAOI(
					result.type === 'Feature'
						? result
						: {
								type: 'Feature',
								geometry: result.geometry,
								properties: result.properties ?? {},
							},
				)
			}
		} catch (err) {
			update('areaFile', undefined)

			update(
				'areaError',
				err instanceof Error
					? err.message
					: 'Invalid area file.',
			)
		}
	}

	const handleReset = () => {
		onFiltersChange(emptyFilters)

		onSelectedCountryChange("")
		onSelectedRegionChange("")

		setDragging(false)
		if (fileInputRef.current) {
			fileInputRef.current.value = ""
		}
	}

	const hasLocation = Boolean(selectedCountry && selectedRegion) || Boolean(filters.areaGeoJSON)
	const canApply = hasLocation && Boolean(filters.startDate) && Boolean(filters.endDate)

	const handleApply = () => {
		const sensorsPayload =
			filters.sensors.length > 0 && filters.sensors.length === phenomena.length
				? []
				: filters.sensors

		onApply({
			...filters,
			sensors: sensorsPayload,
			country: selectedCountry,
			region: selectedRegion,
		})

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

				{/* Top Row */}

				<div className="grid grid-cols-[1fr_1fr_auto] items-end gap-6">

					<div>

						<Label className="mb-2 block">
							Country
						</Label>

						<div className="flex gap-2">

							<Select
								value={selectedCountry}
								onValueChange={(value) => {
									onSelectedCountryChange(value)
									onSelectedRegionChange("")
								}}
								disabled={!!filters.areaGeoJSON}
							>
								<SelectTrigger className="flex-1">
									<SelectValue placeholder="Select country" />
								</SelectTrigger>

								<SelectContent className="max-h-72 overflow-y-auto">
									{countries.map((country) => (
										<SelectItem
											key={country.country}
											value={country.country}
										>
											{country.country}
										</SelectItem>
									))}
								</SelectContent>

							</Select>

							{selectedCountry && (
								<Button
									variant="ghost"
									size="icon"
									onClick={() => {
										onSelectedCountryChange("")
										onSelectedRegionChange("")

									}}
								>
									<X className="h-4 w-4"/>
								</Button>
							)}

						</div>

					</div>

					<div>

						<Label className="mb-2 block">
							Region
						</Label>

						<div className="flex gap-2">

							<Select
								value={selectedRegion}
								onValueChange={onSelectedRegionChange}
								disabled={!selectedCountry || !!filters.areaGeoJSON}
							>
								<SelectTrigger className="flex-1">
									<SelectValue placeholder="Select region"/>
								</SelectTrigger>

								<SelectContent className="max-h-72 overflow-y-auto">

									{(countries.find(
										c => c.country === selectedCountry
									)?.regions ?? []).map((region:string)=>(

										<SelectItem
											key={region}
											value={region}
										>
											{region}
										</SelectItem>

									))}

								</SelectContent>

							</Select>

							{selectedRegion && (

								<Button
									variant="ghost"
									size="icon"
									onClick={() => onSelectedRegionChange("")}
								>
									<X className="h-4 w-4"/>
								</Button>

							)}

						</div>

					</div>

				</div>
				<div className="grid grid-cols-[10rem_1fr] items-center gap-4">
				

				{/* Exposure */}
				<Label>
					Exposure
				</Label>

				<ToggleGroup
					type="single"
					variant="gray"
					value={filters.sensorType}
					onValueChange={(value) => {
						if (value) {
							update(
								"sensorType",
								value as "all" | "indoor" | "outdoor",
							)
						}
					}}
				>
					<ToggleGroupItem value="all">
						All
					</ToggleGroupItem>

					<ToggleGroupItem value="indoor">
						Indoor
					</ToggleGroupItem>
					<ToggleGroupItem value="outdoor">
						Outdoor
					</ToggleGroupItem>
                </ToggleGroup>

			</div>
				{/* Sensor */}

				<div className="grid grid-cols-[10rem_1fr] items-center gap-4">

					<Label>
						Type of sensor
					</Label>
					{/* Sensor */}
					<Popover
						open={sensorOpen}
						onOpenChange={setSensorOpen}
					>

						<PopoverTrigger asChild>

							<Button
								variant="outline"
								className="justify-between font-normal"
							>

								{filters.sensors.length === 0
									? "Select sensor types"
									: `${filters.sensors.length} selected`}

								<ChevronsUpDown className="ml-2 h-4 w-4 opacity-50" />

							</Button>

						</PopoverTrigger>

						<PopoverContent className="w-[420px] p-0">

							<Command>

								<CommandInput placeholder="Search sensor..." />

								<CommandEmpty>
									No sensor found.
								</CommandEmpty>

								<CommandGroup className="max-h-64 overflow-y-auto">

									<CommandItem
										onSelect={() => {

											if (filters.sensors.length === phenomena.length) {

												update("sensors", [])

											} else {

												update("sensors", phenomena)

											}

										}}
									>

										<Check
											className={`mr-2 h-4 w-4 ${
												filters.sensors.length === phenomena.length
													? "opacity-100"
													: "opacity-0"
											}`}
										/>

										Select All

									</CommandItem>

									{phenomena.map((sensor) => (

										<CommandItem
											key={sensor}
											onSelect={() => {

												if (filters.sensors.includes(sensor)) {

													update(
														"sensors",
														filters.sensors.filter(
															x => x !== sensor
														)
													)

												} else {

													update(
														"sensors",
														[...filters.sensors, sensor]
													)

												}

											}}
										>

											<span
											className={`mr-2 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
												filters.sensors.includes(sensor)
													? "border-black bg-black"
													: "border-gray-300 bg-white"
											}`}
										>
											{filters.sensors.includes(sensor)&& (
												<Check className="h-3 w-3 text-white" />
											)}
										    </span>

											{sensor}

										</CommandItem>

									))}

								</CommandGroup>

							</Command>

						</PopoverContent>

					</Popover>

						
				</div>

				{/* Area of Interest */}

				<div className="grid grid-cols-[10rem_1fr] items-start gap-4">

					<Label>
						Area of interest
					</Label>

					<div>

						<div className="flex items-stretch gap-2">

					<input
						ref={fileInputRef}
						type="file"
						hidden
						accept=".shp,.kml,.geojson,.json"
						onChange={(e) => {
							const file = e.target.files?.[0]
							if (file) handleAOIFile(file)
						}}
					/>

					<div
						className={`flex-1 rounded-md border border-dashed px-4 py-2 text-sm text-gray-500 transition
							${dragging
								? "border-blue-500 bg-blue-50"
								: "border-gray-300"
							}`}
						onDragOver={(e) => {
							e.preventDefault()
							setDragging(true)
						}}
						onDragLeave={() => setDragging(false)}
						onDrop={(e) => {
							e.preventDefault()
							setDragging(false)

							const file = e.dataTransfer.files[0]

							if (file) {
								handleAOIFile(file)
							}
						}}
						style={{
							pointerEvents:
								selectedCountry || selectedRegion ? "none" : "auto",
							opacity:
								selectedCountry || selectedRegion ? 0.5 : 1,
						}}
					>
						Drag & drop .shp, .kml, .geojson or .json here
					</div>

					

					<Button
						type="button"
						disabled={!!selectedCountry || !!selectedRegion}
						variant="outline"
						onClick={() => fileInputRef.current?.click()}
						className="shrink-0"
					>
						<Upload className="mr-2 h-4 w-4" />
						Upload
					</Button>

				</div>
					{filters.areaFile && (
						<p className="mt-2 text-sm text-green-600">
							{filters.areaFile.name}
						</p>
					)}

					{filters.areaError && (
						<p className="mt-2 text-sm text-red-600">
							{filters.areaError}
						</p>
					)}
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
								min={MIN_DATE}
								max={filters.endDate || undefined}
								onChange={(e) => {
									const start = e.target.value

									update('startDate', start)

									if (
										filters.endDate &&
										new Date(filters.endDate) >
											new Date(addYears(start, 5))
									) {
										update('endDate', addYears(start, 5))
									}
								}}
								className="pl-10"
							/>
                        </div>

                        <div className="relative">
                            <CalendarIcon className="absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-gray-400" />

                            <Input
								type="date"
								value={filters.endDate}
								min={filters.startDate || MIN_DATE}
								max={
									filters.startDate
										? addYears(filters.startDate, 5)
										: undefined
								}
								onChange={(e) =>
									update('endDate', e.target.value)
								}
								className="pl-10"
							/>
                        </div>

                    </div>
                </div> 

				{/* Active filters summary */}
				<div className="rounded-md border border-black/10 p-3 dark:border-white/10">
					<p className="mb-2 text-sm font-medium">
						Active filters summary
					</p>

					<div className="flex flex-wrap gap-2">
						{selectedCountry && (
							<FilterChip
								label={`Country: ${selectedCountry}`}
								onClear={() => {
									onSelectedCountryChange("")
									onSelectedRegionChange("")
								}}
							/>
						)}

						{selectedRegion && (
							<FilterChip
								label={`Region: ${selectedRegion}`}
								onClear={() => onSelectedRegionChange("")}
							/>
						)}

						<FilterChip
							label={`Exposure: ${
								filters.sensorType === 'all'
									? 'All'
									: filters.sensorType.charAt(0).toUpperCase() +
										filters.sensorType.slice(1)
							}`}
							onClear={
								filters.sensorType !== 'all'
									? () => update('sensorType', 'all')
									: undefined
							}
						/>

						<FilterChip
							label={
								filters.sensors.length === 0
									? 'Sensor: All'
									: `Sensor: ${filters.sensors.length} selected`
							}
							onClear={
								filters.sensors.length > 0
									? () => update('sensors', [])
									: undefined
							}
						/>

						{filters.areaFile && (
							<FilterChip
								label={`AOI: ${filters.areaFile.name}`}
								onClear={() => {
									update('areaFile', undefined)
									update('areaGeoJSON', undefined)
								}}
							/>
						)}

						<FilterChip label={`Start: ${filters.startDate || '--'}`} />

						<FilterChip
							label={`End: ${filters.endDate || '--'}`}
							onClear={
								filters.endDate
									? () => update('endDate', '')
									: undefined
							}
						/>
					</div>
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
					className="h-8 rounded-md px-3 text-sm disabled:opacity-50"
					onClick={handleApply}
					disabled={disabled || !canApply}
				>
					Apply
				</Button>

			</div>

		</div>
	)
}
