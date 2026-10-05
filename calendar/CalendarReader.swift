import AppKit
import EventKit
import Foundation

let arguments = CommandLine.arguments
let authorize = arguments.contains("--authorize")
let outputIndex = arguments.firstIndex(of: "--output")
let output = outputIndex.flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
let store = EKEventStore()
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

func statusName() -> String {
    switch EKEventStore.authorizationStatus(for: .event) {
    case .fullAccess: return "authorized"
    case .notDetermined: return "not_determined"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .writeOnly: return "write_only"
    @unknown default: return "unavailable"
    }
}

func finish() {
    var result: [String: Any] = ["status": statusName(), "events": []]
    if statusName() == "authorized" && !authorize {
        // EventKit expands recurring occurrences within this local-day range.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Europe/Berlin")!
        let start = calendar.startOfDay(for: Date())
        let end = calendar.date(byAdding: .day, value: 1, to: start)!
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let events = store.events(matching: predicate).filter {
            $0.status != .canceled && $0.endDate > start && $0.startDate < end
        }.sorted { $0.startDate < $1.startDate }
        result["events"] = events.map { event -> [String: Any] in
            ["id": event.eventIdentifier ?? "",
             "calendar_id": event.calendar.calendarIdentifier,
             "title": event.title ?? "Termin",
             "start": formatter.string(from: event.startDate),
             "end": formatter.string(from: event.endDate),
             "all_day": event.isAllDay]
        }
    }
    do {
        let data = try JSONSerialization.data(withJSONObject: result)
        if let path = output {
            try data.write(to: URL(fileURLWithPath: path), options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        } else if !authorize {
            FileHandle.standardOutput.write(data)
        }
        if authorize && statusName() == "authorized" {
            let root = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/TickTick Display/.tools")
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            try Data().write(to: root.appendingPathComponent("refresh-now"))
        }
        exit(0)
    } catch {
        fputs("Kalenderdaten konnten nicht ausgegeben werden.\n", stderr)
        exit(1)
    }
}

if authorize && ["not_determined", "write_only"].contains(statusName()) {
    store.requestFullAccessToEvents { _, _ in
        DispatchQueue.main.async { finish() }
    }
    app.run()
} else {
    finish()
}
