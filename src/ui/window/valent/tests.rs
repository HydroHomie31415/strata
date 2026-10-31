// SPDX-License-Identifier: MIT

use super::*;

fn reply(value: serde_json::Value) -> Reply {
    serde_json::from_value(value).expect("provider reply")
}

#[test]
fn phone_navigation_rejects_credentials_controls_and_non_sftp_targets() {
    for uri in [
        "file:///etc",
        "https://phone.test/",
        "sftp:///storage",
        "sftp://user:secret@phone.test/storage",
        "sftp://phone.test/storage?x=1",
        "sftp://phone.test/storage#fragment",
        "sftp://phone.test/\n",
        "not a URI",
    ] {
        assert!(
            phone_location(uri).is_none(),
            "unexpected phone target: {uri:?}"
        );
    }
    assert!(phone_location("sftp://user@phone.test:1739/storage/emulated/0").is_some());
}

#[test]
fn invalidation_cannot_resurrect_a_disconnected_phone_from_an_older_reply() {
    let mut devices = Devices {
        menu: Some(1),
        ..Devices::default()
    };
    devices.reply(reply(
        serde_json::json!({"version":1,"event":"invalidate","revision":2}),
    ));
    devices.reply(reply(serde_json::json!({"version":1,"id":1,"revision":1,"actions":[
        {"id":"browse","label":"Browse phone files","children":[{"id":"phone","label":"Disconnected phone","context":"old"}]}
    ]})));
    assert!(devices.phones.is_empty());
    assert!(devices.menu.is_none());
    assert!(
        !devices.activate(&Phone {
            name: "Disconnected phone".into(),
            action: serde_json::from_value(
                serde_json::json!({"id":"phone","label":"Disconnected phone","context":"old"})
            )
            .expect("valid sidebar fixture")
        })
    );
}

fn phone_row(widget: &gtk::Widget) -> Option<gtk::Button> {
    if let Some(button) = widget.downcast_ref::<gtk::Button>()
        && button
            .child()
            .and_then(|c| c.last_child())
            .and_downcast::<gtk::Label>()
            .is_some_and(|label| label.text() == "Fixture phone")
    {
        return Some(button.clone());
    }
    let mut child = widget.first_child();
    while let Some(current) = child {
        if let Some(button) = phone_row(&current) {
            return Some(button);
        }
        child = current.next_sibling();
    }
    None
}

fn wait_until(mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !ready() {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for phone sidebar state"
        );
        glib::MainContext::default().iteration(false);
        std::thread::sleep(Duration::from_millis(5));
    }
    while glib::MainContext::default().pending() {
        glib::MainContext::default().iteration(false);
    }
}

#[test]
fn sidebar_clicks_mount_and_open_phone_storage_and_follow_connection_and_enable_changes() {
    crate::test_support::gtk_test(
        "ui::window::valent::tests::sidebar_clicks_mount_and_open_phone_storage_and_follow_connection_and_enable_changes",
        || {
            let scratch = tempfile::tempdir().expect("valid sidebar fixture");
            let state = scratch.path().join("state.json");
            let record = scratch.path().join("record.jsonl");
            let fixture = scratch.path().join("provider.py");
            std::fs::write(&fixture, include_str!("tests/provider.py"))
                .expect("valid sidebar fixture");
            let set_state = |revision, connected, mounted| {
                let temporary = state.with_extension("tmp");
                std::fs::write(&temporary, serde_json::json!({"revision":revision,"connected":connected,"mounted":mounted}).to_string()).expect("valid sidebar fixture");
                std::fs::rename(temporary, &state).expect("valid sidebar fixture");
            };
            set_state(0, true, false);
            let root = protocol::config_root().expect("valid sidebar fixture");
            let registration = root.join("valent");
            std::fs::create_dir_all(&registration).expect("valid sidebar fixture");
            let python = glib::find_program_in_path("python3").expect("Python test runtime");
            std::fs::write(
                registration.join("provider.json"),
                serde_json::json!({
                    "version":1,"id":"valent","command":[python,fixture,state,record]
                })
                .to_string(),
            )
            .expect("valid sidebar fixture");
            crate::ui::preferences::PreferenceManager::seed_saved_preferences_for_test();
            let sidebar = super::super::sidebar::build_sidebar(
                super::super::browser_for_window(),
                crate::ui::preferences::PreferenceManager::shared(),
                false,
            );
            let window = gtk::Window::builder().child(&sidebar.widget).build();
            window.present();
            wait_until(|| phone_row(&sidebar.widget).is_some());
            phone_row(&sidebar.widget)
                .expect("valid sidebar fixture")
                .emit_clicked();
            wait_until(|| std::fs::read_to_string(&record).is_ok_and(|text| text.ends_with('\n')));
            let first: serde_json::Value = serde_json::from_str(
                std::fs::read_to_string(&record)
                    .expect("valid sidebar fixture")
                    .lines()
                    .next()
                    .expect("valid sidebar fixture"),
            )
            .expect("valid sidebar fixture");
            assert_eq!(first["action"], "mount-phone");
            assert_eq!(first["context"], "mount-token");
            assert_eq!(first["paths"], serde_json::json!(["/"]));
            assert_eq!(first["background"], true);
            set_state(1, true, true);
            wait_until(|| {
                sidebar
                    .state
                    .valent
                    .borrow()
                    .phones
                    .first()
                    .is_some_and(|p| p.action.id == "open-storage")
                    && phone_row(&sidebar.widget).is_some_and(|row| row.is_sensitive())
            });
            phone_row(&sidebar.widget)
                .expect("valid sidebar fixture")
                .emit_clicked();
            wait_until(|| {
                std::fs::read_to_string(&record)
                    .expect("valid sidebar fixture")
                    .lines()
                    .count()
                    == 2
            });
            let last: serde_json::Value = serde_json::from_str(
                std::fs::read_to_string(&record)
                    .expect("valid sidebar fixture")
                    .lines()
                    .last()
                    .expect("valid sidebar fixture"),
            )
            .expect("valid sidebar fixture");
            assert_eq!(last["action"], "open-storage");
            assert_eq!(last["context"], "storage-token");
            set_state(2, false, false);
            wait_until(|| phone_row(&sidebar.widget).is_none());
            set_state(3, true, false);
            wait_until(|| phone_row(&sidebar.widget).is_some());
            std::fs::write(registration.join(protocol::DISABLED_FILE), "")
                .expect("valid sidebar fixture");
            wait_until(|| {
                phone_row(&sidebar.widget).is_none()
                    && sidebar.state.valent.borrow().client.is_none()
            });
            std::fs::remove_file(registration.join(protocol::DISABLED_FILE))
                .expect("valid sidebar fixture");
            wait_until(|| phone_row(&sidebar.widget).is_some());
            sidebar.disconnect();
            assert!(sidebar.state.valent.borrow().client.is_none());
            window.close();
        },
    );
}
