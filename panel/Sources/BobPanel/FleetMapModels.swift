import Foundation

// The fleet activity map's wire shape: `fleet_activity` on the loopback
// `/api/state`, never on a phone's door. Every field goes through the tolerant
// helpers so one absent key cannot blank the panel, and the section carries
// no timestamp: the pane stamps arrival itself (`docs/fleet-map-contract.md`).

/// One of the seven stations on the ring, in the order the daemon sends them.
struct FleetStation: Decodable, Equatable, Identifiable {
    var id = ""
    var label = ""

    enum CodingKeys: String, CodingKey { case id, label }

    init(id: String = "", label: String = "") {
        self.id = id
        self.label = label
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, "")
        label = c.value(.label, id)
    }
}

/// How a call ended, inferred by the daemon. An unknown word reads as done.
enum FleetCallEnd: String, Equatable {
    case running, done, failed

    static func fromWire(_ word: String) -> FleetCallEnd {
        FleetCallEnd(rawValue: word) ?? .done
    }
}

struct FleetCall: Decodable, Equatable {
    var seq = 0
    var station = "tools"
    var tool = ""
    var end: FleetCallEnd = .done

    enum CodingKeys: String, CodingKey { case seq, station, tool, end }

    init(seq: Int = 0, station: String = "tools", tool: String = "",
         end: FleetCallEnd = .done) {
        self.seq = seq
        self.station = station
        self.tool = tool
        self.end = end
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = c.value(.seq, 0)
        let word: String = c.value(.station, "")
        station = word.isEmpty ? "tools" : word
        tool = c.value(.tool, "")
        end = FleetCallEnd.fromWire(c.value(.end, "done"))
    }
}

struct FleetSessionActivity: Decodable, Equatable {
    var calls: [FleetCall] = []
    var stationCounts: [String: Int] = [:]
    var errors = 0
    /// Failed calls per station, kept by the daemon until the agent's next
    /// prompt (the call window is only eight deep). Absent reads as empty.
    var errorsByStation: [String: Int] = [:]

    enum CodingKeys: String, CodingKey {
        case calls, errors
        case errorsByStation = "errors_by_station"
        case stationCounts = "station_counts"
    }

    init(calls: [FleetCall] = [], stationCounts: [String: Int] = [:],
         errors: Int = 0, errorsByStation: [String: Int] = [:]) {
        self.errorsByStation = errorsByStation
        self.calls = calls
        self.stationCounts = stationCounts
        self.errors = errors
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        calls = c.value(.calls, [])
        stationCounts = c.value(.stationCounts, [:])
        errors = c.value(.errors, 0)
        errorsByStation = c.value(.errorsByStation, [:])
    }
}

/// `available` is stated by the daemon: an older daemon sends no section and
/// an empty object decodes `false`.
struct FleetActivitySection: Decodable, Equatable {
    var available = false
    var stations: [FleetStation] = []
    var sessions: [String: FleetSessionActivity] = [:]

    enum CodingKeys: String, CodingKey { case available, stations, sessions }

    init(available: Bool = false, stations: [FleetStation] = [],
         sessions: [String: FleetSessionActivity] = [:]) {
        self.available = available
        self.stations = stations
        self.sessions = sessions
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        stations = c.value(.stations, [])
        sessions = c.value(.sessions, [:])
    }
}
