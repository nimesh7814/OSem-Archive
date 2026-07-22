import Search from '~/components/search'
import FilterPanel from './filter-panel'
import { DeviceFeatureCollection } from '~/components/search/search-types'
import ArchiveFilterPanel from "./archive-filter-panel"

import type { Feature } from "geojson"

interface NavBarHandlerProps {
    devices: DeviceFeatureCollection
    searchString: string
    archiveMode: boolean
    onZoomToAOI: (feature: Feature) => void
}

export default function NavbarHandler({
    devices,
    searchString,
    archiveMode,
    onZoomToAOI,
}: NavBarHandlerProps) {
	const isSearching = searchString.trim().length >= 2

	if (isSearching) {
		return <Search devices={devices} searchString={searchString} />
	}

	if (archiveMode) {
		return <ArchiveFilterPanel onZoomToAOI={onZoomToAOI} />
	}

	return <FilterPanel />
}
