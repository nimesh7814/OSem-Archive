import Home from './home'
import Menu from './menu'
import NavBar from './nav-bar'

interface HeaderProps {
	devices: any
}

export default function Header(props: HeaderProps) {
	return (
		<div className="items-top pointer-events-none fixed z-10 flex h-14 w-full justify-between gap-4 p-2">
	        <Home deviceCount={999999} measurementCount={999999} />
			<NavBar devices={props.devices} />
			<div className="flex gap-2">
				<Menu devices={props.devices} />
			</div>
		</div>
	)
}
