import Search from '~/components/search'
import FilterPanel from './filter-panel'
import { DeviceFeatureCollection } from '~/components/search/search-types'
import ArchiveFilterPanel from "./archive-filter-panel"

interface NavBarHandlerProps {
    devices: DeviceFeatureCollection
    searchString: string
    archiveMode: boolean
}

export default function NavbarHandler({
    devices,
    searchString,
    archiveMode,
}: NavBarHandlerProps) {
	const isSearching = searchString.trim().length >= 2

	if (isSearching) {
		return <Search devices={devices} searchString={searchString} />
	}

	if (archiveMode) {
		return <ArchiveFilterPanel />
	}

	return <FilterPanel />
}
