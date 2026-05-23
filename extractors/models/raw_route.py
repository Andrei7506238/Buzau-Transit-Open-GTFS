# PDF Row from the Lista-statiilor.pdf file
#   - number_entry: int
#   - route_number: str
#   - from_location: str
#   - intermediary_stop: str (optional)
#   - to_location: str
#   - stations: list of tuples (station_name: str, kilometers_from_previous_station: int)
class RawRoute:
    def __init__(self, number_entry, route_number, from_location, intermediary_stop, to_location, stations):
        self.number_entry = number_entry
        self.route_number = route_number
        self.from_location = from_location
        self.intermediary_stop = intermediary_stop
        self.to_location = to_location
        self.stations = stations