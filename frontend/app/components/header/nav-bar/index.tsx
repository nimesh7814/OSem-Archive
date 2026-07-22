import ArchiveFilterPanel from "./archive-filter-panel"
import { useMediaQuery } from '@mantine/hooks'
import { AnimatePresence, motion } from 'framer-motion'
import { SearchIcon, XIcon } from 'lucide-react'
import { Switch } from '~/components/ui/switch'
import { Button } from '~/components/ui/button'
import { useState, useEffect, useRef, createContext } from 'react'
import { useTranslation } from 'react-i18next'
import { useMap } from 'react-map-gl/maplibre'
import NavbarHandler from './nav-bar-handler'
import FilterVisualization from '~/components/map/filter-visualization'
import { DeviceFeatureCollection } from '~/components/search/search-types'
import { cn } from '~/lib/utils'
import { topbarSurface } from '~/components/map/topbar-styles'

import type { Feature } from "geojson"
import maplibregl from "maplibre-gl"

interface NavBarProps {
    devices: DeviceFeatureCollection
    archiveMode: boolean
    setArchiveMode: React.Dispatch<React.SetStateAction<boolean>>
    onArchiveApply: (filters: any) => void
    onArchiveClear?: () => void
}

export const NavbarContext = createContext({
	open: false,
	setOpen: (_open: boolean) => {},
})

export default function NavBar(props: NavBarProps) {
	console.log("NavBar props:", props.archiveMode)
	const [open, setOpen] = useState(false)
	const inputRef = useRef<HTMLInputElement>(null)
	const [searchString, setSearchString] = useState('')
	const { osem: mapRef } = useMap()
	const [currentAOI, setCurrentAOI] = useState<Feature | null>(null)
	const [appliedSummary, setAppliedSummary] = useState<{
		country?: string
		region?: string
	} | null>(null)

	const zoomToAOI = (feature: Feature) => {
		setCurrentAOI(feature)

		if (!mapRef) return

		if (!['Polygon', 'MultiPolygon'].includes(feature.geometry.type)) return

		const bounds = new maplibregl.LngLatBounds()
		const extendFromCoords = (coords: any): void => {
			if (typeof coords[0] === 'number') {
				bounds.extend(coords as [number, number])
				return
			}
			coords.forEach(extendFromCoords)
		}

		extendFromCoords((feature.geometry as any).coordinates)

		mapRef.fitBounds(bounds, {
			padding: 80,
			duration: 1200,
		})

		const map = mapRef.getMap()

		if (map.getLayer("aoi-fill")) {
			map.removeLayer("aoi-fill")
		}

		if (map.getLayer("aoi-outline")) {
			map.removeLayer("aoi-outline")
		}

		if (map.getSource("aoi")) {
			map.removeSource("aoi")
		}

		map.addSource("aoi", {
			type: "geojson",
			data: feature,
		})

		map.addLayer({
			id: "aoi-fill",
			type: "fill",
			source: "aoi",
			paint: {
				"fill-color": "#2563eb",
				"fill-opacity": 0.2,
			},
		})

		map.addLayer({
			id: "aoi-outline",
			type: "line",
			source: "aoi",
			paint: {
				"line-color": "#2563eb",
				"line-width": 3,
			},
		})
	}

	const handleArchivePanelApply = (filters: any) => {
		setAppliedSummary({
			country: filters.country,
			region: filters.region,
		})
		props.onArchiveApply(filters)
	}

	const handleClear = () => {
		setAppliedSummary(null)
		setCurrentAOI(null)

		if (mapRef) {
			const map = mapRef.getMap()
			if (map.getLayer("aoi-fill")) map.removeLayer("aoi-fill")
			if (map.getLayer("aoi-outline")) map.removeLayer("aoi-outline")
			if (map.getSource("aoi")) map.removeSource("aoi")
		}

		props.onArchiveClear?.()
		setOpen(true)
	}

	const { t } = useTranslation('search')

	useEffect(() => {
		if (!mapRef) return

		const closePanel = () => {
			setOpen(false)
		}

		mapRef.on("click", closePanel)

		return () => {
			mapRef.off("click", closePanel)
		}
	}, [mapRef])

	// register keyboard shortcuts
	useEffect(() => {
		const down = (e: KeyboardEvent) => {
			if (e.key === 'k' && (e.metaKey || e.ctrlKey)) {
				e.preventDefault()
				setOpen((prevState) => !prevState)
			}
			if (e.key === 'Escape') {
				e.preventDefault()
				setOpen(false)
			}
		}
		document.addEventListener('keydown', down)
		return () => document.removeEventListener('keydown', down)
	}, [])

	useEffect(() => {
		if (open) {
			inputRef.current?.focus()
		} else {
			inputRef.current?.blur()
			setSearchString('')
		}
	}, [open])

	const isDesktop = useMediaQuery('(min-width: 768px)')

	return (
		<div className="pointer-events-auto relative w-full max-w-176">
			<motion.div
				layout
				className={cn(
					topbarSurface({ shape: 'panel' }),
					'w-full overflow-hidden px-3 md:px-4',
				)}
				animate={{
					borderRadius: 16,
				}}
				transition={{
					layout: {
						duration: 0.24,
						ease: [0.22, 1, 0.36, 1],
					},
					borderRadius: {
						duration: 0.2,
						ease: [0.22, 1, 0.36, 1],
					},
				}}
			>
				{props.archiveMode ? (

					<div className="flex h-14 items-center justify-between gap-6" onClick={() => setOpen(true)}>

						<div className="flex flex-1 items-center gap-3 overflow-hidden cursor-pointer">

							{appliedSummary && (appliedSummary.country || appliedSummary.region) && (
								<span className="truncate text-sm text-gray-700 dark:text-zinc-300">
									{[appliedSummary.country, appliedSummary.region]
										.filter(Boolean)
										.join(' / ')}
								</span>
							)}

						</div>

						<div className="flex items-center gap-3 border-l border-black/10 pl-4" onClick={(e) => e.stopPropagation()}>

							{appliedSummary && (
								<>
									<Button
										type="button"
										variant="ghost"
										size="sm"
										className="h-7 px-2 text-xs"
										onClick={() => setOpen(true)}
									>
										Edit
									</Button>

									<Button
										type="button"
										variant="ghost"
										size="sm"
										className="h-7 px-2 text-xs"
										onClick={handleClear}
									>
										Clear
									</Button>
								</>
							)}

							<span className="text-sm">
								Archive
							</span>

							<Switch
								checked={props.archiveMode}
								onCheckedChange={(checked) => {
									props.setArchiveMode(checked)

									if (!checked) {
										setOpen(false)
									}
								}}
							/>

						</div>

					</div>

				) : (

					<div className="flex h-11 w-full items-center gap-2 text-black md:gap-4 dark:text-zinc-200">

						<SearchIcon className="h-6 w-6 shrink-0 text-red-600"/>

						<input
							ref={inputRef}
							placeholder={t('placeholder') || undefined}
							onFocus={() => setOpen(true)}
							onChange={(e) => setSearchString(e.target.value)}
							className="h-full w-full flex-1 border-none bg-transparent focus:border-none focus:ring-0 focus:outline-hidden dark:text-zinc-200"
							value={searchString}
						/>

						{!open && (
							<span className="hidden flex-none text-xs font-semibold text-gray-400 md:block">
								<kbd>ctrl</kbd> + <kbd>K</kbd>
							</span>
						)}

						{open && (
							<button
								type="button"
								onClick={() => {
									setSearchString("")
									setOpen(false)
								}}
							>
								<XIcon className="h-5 w-5" />
							</button>
						)}

						<div className="flex items-center gap-2 border-l border-black/10 pl-3">

							<span className="text-xs">
								Archive
							</span>

							<Switch
								checked={props.archiveMode}
								onCheckedChange={(checked) => {
									props.setArchiveMode(checked)

									if (checked) {
										setOpen(true)
									}
								}}
							/>

						</div>

					</div>

				)}

				<NavbarContext.Provider value={{ open, setOpen }}>
					<AnimatePresence initial={false}>
						{open && (
							<motion.div
								key="search-results"
								className="overflow-hidden"
								initial={{
									opacity: 0,
									height: 0,
									y: -4,
								}}
								animate={{
									opacity: 1,
									height: 'auto',
									y: 0,
								}}
								exit={{
									opacity: 0,
									height: 0,
									y: -4,
								}}
								transition={{
									duration: 0.22,
									ease: [0.22, 1, 0.36, 1],
								}}
							>
								<div className="pt-2">
									{props.archiveMode ? (
										<ArchiveFilterPanel
											onZoomToAOI={zoomToAOI}
											onApply={handleArchivePanelApply}
										/>
									) : (
										<NavbarHandler
											devices={props.devices}
											searchString={searchString}
											archiveMode={props.archiveMode}
											onZoomToAOI={zoomToAOI}
										/>
									)}

								</div>
							</motion.div>
						)}
					</AnimatePresence>
				</NavbarContext.Provider>
			</motion.div>
			{!open && isDesktop && (
				<div className="flex w-full items-center justify-center">
					<FilterVisualization />
				</div>
			)}
		</div>
	)
}