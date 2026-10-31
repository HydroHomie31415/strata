// SPDX-License-Identifier: MIT

//! Phone rows reuse the opt-in Valent provider's validated menu and activation
//! protocol. Mounting, credentials and exported SFTP URI validation stay there.

use std::{
    rc::{Rc, Weak},
    sync::mpsc,
    time::{Duration, Instant},
};

use gtk::{gio, glib, prelude::*};

use crate::services::file_providers::{
    self as protocol, Client, MenuAction, OutcomeStatus, Registration, Reply, Request, Update,
};

use super::SidebarState;

const REFRESH: Duration = Duration::from_secs(5);
const OPEN_INTERFACE: &str = "io.github.lgse.Strata.ValentSidebar";

struct Endpoint {
    connection: gio::DBusConnection,
    registration: Option<gio::RegistrationId>,
    path: String,
}

impl Drop for Endpoint {
    fn drop(&mut self) {
        if let Some(registration) = self.registration.take() {
            let _ = self.connection.unregister_object(registration);
        }
    }
}

fn phone_location(uri: &str) -> Option<crate::model::Location> {
    if uri.len() > 16384 || uri.chars().any(char::is_control) {
        return None;
    }
    let parsed = glib::Uri::parse(uri, glib::UriFlags::HAS_PASSWORD).ok()?;
    if parsed.scheme() != "sftp"
        || parsed.host().is_none_or(|host| host.is_empty())
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return None;
    }
    crate::adapters::location_for_file(&gio::File::for_uri(uri))
}

impl Endpoint {
    fn new(
        connection: gio::DBusConnection,
        open: impl Fn(crate::model::Location) -> bool + 'static,
    ) -> Result<Self, glib::Error> {
        let path = format!(
            "/io/github/lgse/Strata/ValentSidebar/{}",
            glib::uuid_string_random().replace('-', "_")
        );
        let node = gio::DBusNodeInfo::for_xml(
            "<node><interface name='io.github.lgse.Strata.ValentSidebar'><method name='Open'><arg name='uri' type='s' direction='in'/></method></interface></node>",
        )?;
        let interface = node
            .lookup_interface(OPEN_INTERFACE)
            .expect("sidebar interface");
        let registration = connection
            .register_object(&path, &interface)
            .method_call(move |_, _, _, _, method, parameters, invocation| {
                let location = parameters
                    .get::<(String,)>()
                    .and_then(|(uri,)| phone_location(&uri));
                if method == "Open" && location.is_some_and(&open) {
                    invocation.return_value(None);
                } else {
                    invocation.return_error(
                        gio::DBusError::AccessDenied,
                        "No pending phone navigation or invalid SFTP location",
                    );
                }
            })
            .build()?;
        Ok(Self {
            connection,
            registration: Some(registration),
            path,
        })
    }
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct Phone {
    name: String,
    action: MenuAction,
}

pub(super) struct Devices {
    client: Option<Client>,
    command: Vec<String>,
    discovery: Option<mpsc::Receiver<Option<Registration>>>,
    next_discovery: Instant,
    next_menu: Instant,
    serial: u64,
    menu: Option<u64>,
    activation: Option<u64>,
    revision: u64,
    endpoint: Option<Endpoint>,
    watching: bool,
    opening: Option<Instant>,
    pub(super) phones: Vec<Phone>,
}

impl Default for Devices {
    fn default() -> Self {
        Self {
            client: None,
            command: Vec::new(),
            discovery: None,
            next_discovery: Instant::now(),
            next_menu: Instant::now(),
            serial: 0,
            menu: None,
            activation: None,
            revision: 0,
            endpoint: None,
            watching: false,
            opening: None,
            phones: Vec::new(),
        }
    }
}

pub(super) struct Watch {
    timer: Option<glib::SourceId>,
    state: Weak<SidebarState>,
}

impl Drop for Watch {
    fn drop(&mut self) {
        if let Some(timer) = self.timer.take() {
            timer.remove();
        }
        if let Some(state) = self.state.upgrade() {
            *state.valent.borrow_mut() = Devices::default();
        }
    }
}

pub(super) fn watch(state: &Rc<SidebarState>) -> Option<Watch> {
    if state.local_only || state.browser.is_chooser_mode() {
        return None;
    }
    let weak = Rc::downgrade(state);
    state.valent.borrow_mut().watching = true;
    let target = weak.clone();
    glib::MainContext::default().spawn_local(async move {
        let Ok(connection) = gio::bus_get_future(gio::BusType::Session).await else {
            return;
        };
        let Some(state) = target.upgrade() else {
            return;
        };
        if !state.valent.borrow().watching {
            return;
        }
        let endpoint = Endpoint::new(connection, move |location| {
            let Some(state) = target.upgrade() else {
                return false;
            };
            let pending = state
                .valent
                .borrow_mut()
                .opening
                .take()
                .is_some_and(|at| at.elapsed() < Duration::from_secs(35));
            if pending {
                state.browser.navigate_location(location, false);
            }
            pending
        });
        if let Ok(endpoint) = endpoint {
            state.valent.borrow_mut().endpoint = Some(endpoint);
        }
    });
    let state_for_timer = weak.clone();
    let timer = glib::timeout_add_local(Duration::from_millis(100), move || {
        let Some(state) = state_for_timer.upgrade() else {
            return glib::ControlFlow::Break;
        };
        let (changed, error) = state.valent.borrow_mut().tick();
        if changed {
            state.queue_rebuild();
        }
        if let Some(message) = error {
            super::show_error_dialog(
                &state.view.widget(),
                &crate::i18n::tr("Unable to open location"),
                &message,
            );
        }
        glib::ControlFlow::Continue
    });
    Some(Watch {
        timer: Some(timer),
        state: weak,
    })
}

pub(super) fn append_rows(state: &Rc<SidebarState>) {
    let devices = state.valent.borrow();
    for phone in &devices.phones {
        let row = super::sidebar_button(crate::assets::icons::SMARTPHONE, &phone.name);
        row.set_sensitive(devices.activation.is_none());
        let phone = phone.clone();
        let weak = Rc::downgrade(state);
        row.connect_clicked(move |row| {
            let Some(state) = weak.upgrade() else {
                return;
            };
            let sent = state.valent.borrow_mut().activate(&phone);
            if sent {
                super::select_sidebar_row(&state.widget, row);
                state.queue_rebuild();
            }
        });
        state.append_sidebar_row(&row);
    }
}

fn phones(actions: &[MenuAction]) -> Vec<Phone> {
    fn first_leaf(action: &MenuAction) -> Option<&MenuAction> {
        match &action.children {
            Some(children) if !children.is_empty() => children.iter().find_map(first_leaf),
            _ => Some(action),
        }
    }
    actions
        .iter()
        .find(|action| action.id == "browse")
        .and_then(|action| action.children.as_ref())
        .into_iter()
        .flatten()
        .filter_map(|device| {
            Some(Phone {
                name: device.label.clone(),
                action: first_leaf(device)?.clone(),
            })
        })
        .collect()
}

impl Devices {
    fn install(&mut self, mut registration: Option<Registration>) {
        let Some(endpoint) = &self.endpoint else {
            return;
        };
        let command = registration
            .as_ref()
            .map(|r| r.manifest.command.clone())
            .unwrap_or_default();
        if command == self.command {
            return;
        }
        self.command = command;
        if let Some(registration) = &mut registration {
            registration.manifest.command.extend([
                "--open-destination".into(),
                endpoint
                    .connection
                    .unique_name()
                    .expect("session bus unique name")
                    .to_string(),
                endpoint.path.clone(),
            ]);
        }
        self.client = registration.map(protocol::start);
        self.phones.clear();
        self.menu = None;
        self.activation = None;
        self.revision = 0;
        self.opening = None;
        self.next_menu = Instant::now();
    }

    fn send(&mut self, method: &str, action: Option<&MenuAction>) -> Option<u64> {
        self.serial += 1;
        let request = Request {
            version: 1,
            id: self.serial,
            method: method.into(),
            paths: vec!["/".into()],
            background: true,
            action: action.map(|a| a.id.clone()),
            context: action.and_then(|a| a.context.clone()),
        };
        self.client.as_ref()?.requests.try_send(request).ok()?;
        Some(self.serial)
    }

    fn activate(&mut self, phone: &Phone) -> bool {
        if self.activation.is_some() || !self.phones.contains(phone) {
            return false;
        }
        self.activation = self.send("activate", Some(&phone.action));
        if self.activation.is_some() {
            self.opening = Some(Instant::now());
        }
        self.activation.is_some()
    }

    fn reply(&mut self, reply: Reply) -> Option<String> {
        if reply.event.as_deref() == Some("invalidate") {
            self.revision = self.revision.max(reply.revision.unwrap_or(0));
            self.phones.clear();
            self.next_menu = Instant::now();
        } else if reply.id.is_some() && reply.id == self.menu {
            self.menu = None;
            if reply.error.is_none() && reply.revision.unwrap_or(0) >= self.revision {
                self.phones = phones(reply.actions.as_deref().unwrap_or_default());
            } else {
                self.phones.clear();
                self.next_menu = Instant::now();
            }
        } else if reply.id.is_some() && reply.id == self.activation {
            self.activation = None;
            self.next_menu = Instant::now();
            if reply.error.is_some()
                || !reply
                    .outcome
                    .is_some_and(|o| o.status == OutcomeStatus::Accepted)
            {
                self.opening = None;
                return Some(if reply.message.is_empty() {
                    crate::i18n::tr("The device is not available")
                } else {
                    reply.message
                });
            }
        }
        None
    }

    fn tick(&mut self) -> (bool, Option<String>) {
        let before = self.phones.clone();
        let was_busy = self.activation.is_some();
        let mut error = None;
        if let Some(result) = self.discovery.as_ref().and_then(|rx| rx.try_recv().ok()) {
            self.discovery = None;
            self.install(result);
        }
        if self.discovery.is_none() && Instant::now() >= self.next_discovery {
            let (tx, rx) = mpsc::channel();
            self.discovery = Some(rx);
            self.next_discovery = Instant::now() + REFRESH;
            std::thread::spawn(move || {
                let registration = protocol::config_root().and_then(|root| {
                    protocol::discover(&root)
                        .into_iter()
                        .find(|r| r.manifest.id == "valent")
                });
                let _ = tx.send(registration);
            });
        }
        if let Some(client) = &self.client {
            let (updates, closed) = client.updates.drain();
            for update in updates {
                match update {
                    Update::Reply(reply) => {
                        error = self.reply(*reply).or(error);
                    }
                    Update::Offline { .. } => {
                        if self.activation.take().is_some() {
                            error = Some(crate::i18n::tr("The device is not available"));
                        }
                        self.menu = None;
                        self.phones.clear();
                        self.revision = 0;
                        self.opening = None;
                        self.next_menu = Instant::now() + REFRESH;
                    }
                }
            }
            if closed {
                self.client = None;
                self.command.clear();
                self.phones.clear();
                self.menu = None;
                self.activation = None;
            }
        }
        if self.client.is_some()
            && self.menu.is_none()
            && self.activation.is_none()
            && Instant::now() >= self.next_menu
        {
            self.menu = self.send("menu", None);
            self.next_menu = Instant::now() + REFRESH;
        }
        (
            before != self.phones || was_busy != self.activation.is_some(),
            error,
        )
    }
}

#[cfg(test)]
mod tests;
