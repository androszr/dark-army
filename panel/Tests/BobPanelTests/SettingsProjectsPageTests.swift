import XCTest
@testable import BobPanel

/// Projects as a list and a detail, split from the `submenu:projects` row the
/// tree already builds.
final class SettingsProjectsPageTests: XCTestCase {

    private func projectsRow(_ enrollment: Enrollment) -> SettingsRow {
        SettingsMenuModel.rows(
            settings: DaemonClient.Settings(), context: DaemonClient.PanelContext(),
            enrollment: enrollment, recordingShortcut: false, unenrolArmed: nil)
            .first { $0.id == "submenu:projects" }!
    }

    private func enrolled(_ projects: [(String, String)]) -> Enrollment {
        var enrollment = Enrollment()
        enrollment.available = true
        enrollment.enrolled = projects.map { EnrolledProject(root: $0.0, label: $0.1) }
        return enrollment
    }

    func testTwoProjectsSharingALabelAreTwoItemsKeyedByRoot() {
        let layout = SettingsProjectsPage.build(
            projectsRow: projectsRow(enrolled([("/a/app", "app"), ("/b/app", "app")])),
            selectedRoot: nil)
        XCTAssertEqual(layout.projects.map(\.root), ["/a/app", "/b/app"])
        XCTAssertEqual(layout.projects.map(\.label), ["app", "app"])
        XCTAssertEqual(Set(layout.projects.map(\.id)).count, 2)
    }

    func testTheSelectionFallsBackToTheFirst() {
        let row = projectsRow(enrolled([("/a/one", "one"), ("/b/two", "two")]))
        XCTAssertEqual(SettingsProjectsPage.build(projectsRow: row, selectedRoot: nil)
            .selected?.root, "/a/one")
        XCTAssertEqual(SettingsProjectsPage.build(projectsRow: row, selectedRoot: "/gone")
            .selected?.root, "/a/one")
        XCTAssertEqual(SettingsProjectsPage.build(projectsRow: row, selectedRoot: "/b/two")
            .selected?.root, "/b/two")
    }

    func testNoProjectsSelectsNothingAndKeepsTheEnrolmentLineAndTheEnrolButton() {
        for enrollment in [Enrollment(), enrolled([])] {
            let layout = SettingsProjectsPage.build(projectsRow: projectsRow(enrollment),
                                                    selectedRoot: nil)
            XCTAssertTrue(layout.projects.isEmpty)
            XCTAssertNil(layout.selected)
            XCTAssertEqual(layout.listFooter.map(\.id), ["info:enrollment", "custom:enrolFolder"])
        }
        // With projects, the enrol button is still under the list.
        let some = SettingsProjectsPage.build(
            projectsRow: projectsRow(enrolled([("/a/one", "one")])), selectedRoot: nil)
        XCTAssertEqual(some.listFooter.map(\.id), ["custom:enrolFolder"])
    }

    func testEachItemStartsWithItsRootAndEndsWithUnenrol() {
        let layout = SettingsProjectsPage.build(
            projectsRow: projectsRow(enrolled([("/a/one", "one"), ("/b/two", "two")])),
            selectedRoot: nil)
        for item in layout.projects {
            XCTAssertEqual(item.rows.first?.id, "info:project-root:\(item.root)")
            XCTAssertEqual(item.rows.last?.id, "custom:unenrol:\(item.root)")
        }
    }

    func testAnythingButASubmenuYieldsAnEmptyLayout() {
        let layout = SettingsProjectsPage.build(
            projectsRow: SettingsRow(id: "info:x", title: "x", kind: .info),
            selectedRoot: nil)
        XCTAssertEqual(layout, SettingsProjectsPage.Layout(projects: [], listFooter: [],
                                                           selected: nil))
    }

    /// The detail pane and the full-width foot split a project's rows with
    /// nothing dropped and nothing drawn twice; the model table and Un-enrol
    /// are the foot's.
    func testThePartsPlaceEveryRowOnce() {
        var enrollment = enrolled([("/a/one", "one")])
        enrollment.available = true
        let item = SettingsProjectsPage.build(projectsRow: projectsRow(enrollment),
                                              selectedRoot: nil).selected!
        let parts = SettingsProjectParts(item: item)
        XCTAssertEqual(parts.rootRow?.id, "info:project-root:/a/one")
        XCTAssertEqual(parts.unenrol.map(\.id), ["custom:unenrol:/a/one"])
        let drawn = [parts.rootRow].compactMap { $0 } + parts.pack + parts.rest
            + [parts.models].compactMap { $0 } + parts.unenrol
        let expected = item.rows.filter { row in
            if case .divider = row.kind { return false }
            return true
        }
        XCTAssertEqual(drawn.map(\.id).sorted(), expected.map(\.id).sorted())
        XCTAssertEqual(Set(drawn.map(\.id)).count, drawn.count)
    }
}
