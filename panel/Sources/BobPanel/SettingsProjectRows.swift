import Foundation

// The Projects group: one submenu per enrolled root with its agent-pack
// rows, plus Enrol a folder…. Split out of `SettingsMenuModel.swift` on
// 20 Sep 2026.
extension SettingsMenuModel {
    static let packProfiles: [(id: String, label: String)] = [
        ("web", "Web"),
        ("ios", "iOS"),
        ("both", "Both"),
    ]

    static func projectRows(_ enrollment: Enrollment,
                                    unenrolArmed: String?,
                                    pack: DaemonClient.AgentPack,
                                    packArmed: String?,
                                    stopSyncArmed: String?,
                                    agentModels: DaemonClient.AgentModels = DaemonClient.AgentModels()
    ) -> [SettingsRow] {
        var rows: [SettingsRow] = []
        if !enrollment.available {
            rows.append(info("This daemon does not report enrolment",
                             id: "info:enrollment"))
        } else if enrollment.enrolled.isEmpty {
            rows.append(info("No project is enrolled", id: "info:enrollment"))
        } else {
            for project in enrollment.enrolled {
                let armed = unenrolArmed == project.root
                var nested: [SettingsRow] = [
                    info(project.root, id: "info:project-root:\(project.root)"),
                ]
                nested.append(contentsOf: packRows(
                    for: project.root, pack: pack,
                    packArmed: packArmed, stopSyncArmed: stopSyncArmed))
                nested.append(SettingsRow(
                    id: "custom:knowledge:\(project.root)",
                    title: "Knowledge",
                    kind: .custom(.knowledge(root: project.root))))
                if agentModels.available {
                    nested.append(submenu(
                        "Agent models",
                        id: projectAgentModelsPrefix + project.root,
                        tooltip: "This project's own choices. Inherit keeps "
                            + "the machine-wide setting for that row.",
                        rows: agentModelRows(agentModels, root: project.root)))
                }
                nested.append(divider("unenrol:\(project.root)"))
                nested.append(SettingsRow(
                    id: "custom:unenrol:\(project.root)",
                    title: armed ? "Un-enrol — really?" : "Un-enrol",
                    kind: .custom(.unenrol(root: project.root))))
                rows.append(submenu(
                    project.label,
                    id: "submenu:project:\(project.root)",
                    rows: nested))
            }
        }
        rows.append(divider("projects-end"))
        rows.append(SettingsRow(
            id: "custom:enrolFolder",
            title: "Enrol a folder…",
            kind: .custom(.enrolFolder)))
        return rows
    }

    static func packRows(
        for root: String,
        pack: DaemonClient.AgentPack,
        packArmed: String?,
        stopSyncArmed: String?
    ) -> [SettingsRow] {
        guard pack.available else { return [] }
        if pack.isSelf(root) {
            return [info(
                "Dark Army does not install the pack into its own project",
                id: "info:pack-self:\(root)")]
        }
        if pack.isInstalling(root) {
            return [info("Installing the agent pack…",
                         id: "info:pack-installing:\(root)")]
        }
        var rows: [SettingsRow] = []
        let installed = pack.project(root: root)
        if let installed {
            rows.append(info(
                packSyncLine(installed),
                id: "info:pack-status:\(root)"))
        }
        let submenuTitle = installed == nil
            ? "Install agent pack" : "Re-install agent pack"
        rows.append(submenu(
            submenuTitle,
            id: "submenu:pack:\(root)",
            rows: (pack.profiles.isEmpty ? packProfiles : pack.profiles).map { option in
                let key = "\(root)|\(option.id)"
                let title = packArmed == key
                    ? "\(option.label) — overwrite files?" : option.label
                return SettingsRow(
                    id: "custom:installPack:\(root):\(option.id)",
                    title: title,
                    kind: .custom(.installPack(root: root, profile: option.id)))
            }))
        if installed != nil {
            let stopArmed = stopSyncArmed == root
            rows.append(SettingsRow(
                id: "custom:stopPackSync:\(root)",
                title: stopArmed ? "Stop syncing — really?" : "Stop syncing",
                kind: .custom(.stopPackSync(root: root))))
        }
        return rows
    }

    static func packSyncLine(_ project: DaemonClient.AgentPackProject,
                                     now: Date = Date()) -> String {
        let name = project.profile.isEmpty ? "pack" : project.profile
        if project.lastResult != "ok" && !project.lastResult.isEmpty {
            return "Agent pack: \(name) · last sync: \(project.lastResult)"
        }
        guard project.lastSyncAt > 0 else {
            return "Agent pack: \(name)"
        }
        let age = now.timeIntervalSince1970 - project.lastSyncAt
        if age < 15 {
            return "Agent pack: \(name) · synced just now"
        }
        return "Agent pack: \(name) · synced \(relativeSpan(age)) ago"
    }
}
