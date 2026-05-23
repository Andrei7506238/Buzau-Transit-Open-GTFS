# Schedule Entry (subrow in the schedule table)
#   - way_departure: str
#   - way_arrival: str
#   - back_departure: str
#   - back_arrival: str
#   - days_of_operation: list of int (1 for Monday, 2 for Tuesday, ..., 7 for Sunday)
#   - only_for_school_days: bool
#   - notes: str (optional)
class RawScheduleRow:
    def __init__(self, way_departure, way_arrival, back_departure, back_arrival, days_of_operation, only_for_school_days, notes):
        self.way_departure = way_departure
        self.way_arrival = way_arrival
        self.back_departure = back_departure
        self.back_arrival = back_arrival
        self.days_of_operation = days_of_operation
        self.only_for_school_days = only_for_school_days
        self.notes = notes

# PDF Row from the Program-transport.pdf file
#   - network_id: int
#   - group_id: int
#   - route_number: str
#   - from_location: str
#   - intermediary_stop: str (optional)
#   - to_location: str
#   - kilometers: int
#   - planned_trips: int
#   - schedule_entries: list of RawScheduleRow
class RawSchedule:
    def __init__(self, network_id, group_id, route_number, from_location, intermediary_stop, to_location, kilometers, planned_trips, schedule_entries):
        self.network_id = network_id
        self.group_id = group_id
        self.route_number = route_number
        self.from_location = from_location
        self.intermediary_stop = intermediary_stop
        self.to_location = to_location
        self.kilometers = kilometers
        self.planned_trips = planned_trips
        self.schedule_entries = schedule_entries