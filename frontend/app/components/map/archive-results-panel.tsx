import { useMemo, useState } from 'react'

interface ArchiveResultsPanelProps {
	results: any
	loading?: boolean
	onDownload: (format: 'csv' | 'geojson') => void
	downloadStatus: 'idle' | 'queued' | 'running' | 'done' | 'failed'
	downloadError?: string | null
	onClose: () => void
}

function extractSensorSeries(results: any): Array<{
	id: string
	title: string
	unit?: string
	sensorCount: number
	points: Array<{ time: string; value: number }>
}> {
	if (!results?.boxes || !Array.isArray(results.boxes)) return []

	// Group by title AND unit together — two sensors sharing a generic
	// title like "Anderer" but measuring different things on different
	// scales must not be averaged into the same chart.
	const byGroup = new Map<
		string,
		{
			title: string
			unit?: string
			sensorIds: Set<string>
			buckets: Map<string, { sum: number; count: number }>
		}
	>()

	for (const box of results.boxes) {
		const sensors = box.sensors ?? []

		for (const sensor of sensors) {
			const title = sensor.title ?? sensor.sensorType ?? 'Unknown'
			const unit = sensor.unit ?? ''
			const groupKey = `${title}__${unit}`
			const rawPoints = sensor.measurements ?? []

			if (!byGroup.has(groupKey)) {
				byGroup.set(groupKey, {
					title,
					unit: sensor.unit,
					sensorIds: new Set(),
					buckets: new Map(),
				})
			}

			const entry = byGroup.get(groupKey)!
			entry.sensorIds.add(sensor._id)

			for (const p of rawPoints) {
				const time = p.time
				const value = p.value ?? p.avgValue
				if (typeof value !== 'number' || !time) continue

				const bucket = entry.buckets.get(time) ?? { sum: 0, count: 0 }
				bucket.sum += value
				bucket.count += 1
				entry.buckets.set(time, bucket)
			}
		}
	}

	const series = Array.from(byGroup.entries()).map(([groupKey, entry]) => ({
		id: groupKey,
		title: entry.title,
		unit: entry.unit,
		sensorCount: entry.sensorIds.size,
		points: Array.from(entry.buckets.entries())
			.map(([time, { sum, count }]) => ({ time, value: sum / count }))
			.sort((a, b) => a.time.localeCompare(b.time)),
	}))

	series.sort((a, b) => b.sensorCount - a.sensorCount)

	return series
}
	

function formatTimeLabel(iso: string) {
	const d = new Date(iso)
	if (isNaN(d.getTime())) return iso
	return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function MiniBarChart({
	points,
	unit,
}: {
	points: Array<{ time: string; value: number }>
	unit?: string
}) {
	if (points.length === 0) {
		return <p className="text-xs text-gray-400">No data points.</p>
	}

	const shown = points.slice(-40)
	const values = shown.map((p) => p.value).filter((v) => typeof v === 'number')
	const max = Math.max(...values, 1)
	const min = Math.min(...values, 0)
	const unitSuffix = unit ? ` ${unit}` : ''

	return (
		<div>
			<div className="flex h-16 items-end gap-0.5">
				{shown.map((p, i) => (
					<div
						key={i}
						title={`${formatTimeLabel(p.time)}: ${p.value.toFixed(2)}${unitSuffix}`}
						className="flex-1 rounded-t bg-blue-500/70 transition-colors hover:bg-blue-500"
						style={{ height: `${Math.max((p.value / max) * 100, 2)}%` }}
					/>
				))}
			</div>

			<div className="mt-1 flex justify-between text-[10px] text-gray-400">
				<span>min {min.toFixed(1)}{unitSuffix}</span>
				<span>max {max.toFixed(1)}{unitSuffix}</span>
			</div>

			<div className="flex justify-between text-[10px] text-gray-300 dark:text-gray-600">
				<span>{formatTimeLabel(shown[0].time)}</span>
				<span>{formatTimeLabel(shown[shown.length - 1].time)}</span>
			</div>
		</div>
	)
}

export default function ArchiveResultsPanel({
	results,
	loading,
	onDownload,
	downloadStatus,
	downloadError,
	onClose,
}: ArchiveResultsPanelProps) {
	const series = useMemo(() => extractSensorSeries(results), [results])
	const [downloadFormat, setDownloadFormat] = useState<'csv' | 'geojson'>('csv')

	if (!results && !loading) return null

	return (
		<div className="pointer-events-auto absolute top-20 right-3 z-20 w-80 max-h-[70vh] overflow-y-auto rounded-xl border border-black/10 bg-white p-4 shadow-lg dark:border-white/10 dark:bg-zinc-900">
			<div className="mb-3 flex items-center justify-between">
				<h3 className="text-sm font-semibold">Query results</h3>
				<button type="button" onClick={onClose} className="text-xs text-gray-400 hover:text-gray-600">
					Close
				</button>
			</div>

            {results?.note && (
				<p className="mb-3 rounded-md bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">
					{results.note}
				</p>
			)}

			{loading && <p className="text-sm text-gray-500">Loading measurements…</p>}

			{!loading && series.length === 0 && (
				<p className="text-sm text-gray-500">No data available for selected filters.</p>
			)}

			<div className="mb-4 border-b border-black/5 pb-3 dark:border-white/10">
				<div className="mb-2 flex gap-2">
					<button
						type="button"
						onClick={() => setDownloadFormat('csv')}
						className={`flex-1 rounded-md border px-2 py-1 text-xs ${
							downloadFormat === 'csv'
								? 'border-black bg-black text-white'
								: 'border-black/10 text-gray-600'
						}`}
					>
						CSV
					</button>
					<button
						type="button"
						onClick={() => setDownloadFormat('geojson')}
						className={`flex-1 rounded-md border px-2 py-1 text-xs ${
							downloadFormat === 'geojson'
								? 'border-black bg-black text-white'
								: 'border-black/10 text-gray-600'
						}`}
					>
						GeoJSON
					</button>
				</div>

				<button
					type="button"
					onClick={() => onDownload(downloadFormat)}
					disabled={downloadStatus === 'queued' || downloadStatus === 'running'}
					className="w-full rounded-md bg-black px-3 py-2 text-sm text-white disabled:opacity-50"
				>
					{downloadStatus === 'idle' && `Download all data (${downloadFormat.toUpperCase()})`}
					{downloadStatus === 'queued' && 'Queued — waiting to start…'}
					{downloadStatus === 'running' && 'Preparing your export…'}
					{downloadStatus === 'done' && "Download started — click again if it didn't open"}
					{downloadStatus === 'failed' && 'Failed — try again'}
				</button>

				{downloadError && <p className="mt-2 text-xs text-red-600">{downloadError}</p>}
			</div>

			<div className="flex flex-col divide-y divide-black/5 dark:divide-white/10">
				{series.slice(0, 12).map((sensor) => (
					<div key={sensor.id} className="py-3 first:pt-0 last:pb-0">
						<p className="mb-0.5 text-xs font-semibold">
							{sensor.title}
							{sensor.unit ? ` (${sensor.unit})` : ''}
						</p>
						<p className="mb-1.5 text-[10px] text-gray-400">
							{sensor.sensorCount} sensor{sensor.sensorCount !== 1 ? 's' : ''} · {sensor.points.length} readings · value over time, left = oldest
						</p>
						<MiniBarChart points={sensor.points} unit={sensor.unit} />
					</div>
				))}

				{series.length > 12 && (
					<p className="pt-3 text-xs text-gray-400">
						+{series.length - 12} more phenomena not shown
					</p>
				)}
			</div>
		</div>

	)
}