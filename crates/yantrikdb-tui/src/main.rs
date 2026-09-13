//! `yantrikdb-tui <store.db>`: a terminal explorer for one YantrikDB store.
//!
//! Three panes: namespaces, memories (newest first, or the results of a
//! semantic search), and an inspector for the selected memory with its
//! text, metadata, linked entities, the claims it backs, and its revision
//! history; the namespace's tasks sit under the inspector.
//!
//! READ-ONLY BY CONSTRUCTION. Every read goes through the engine's own
//! non-reinforcing paths (`recall(.., skip_reinforce = true, ..)`,
//! `list_memories`, `get`, the `engine::inspect` reads), so opening a store
//! here leaves no access-count trace and writes nothing. The store is
//! opened with the engine's own SQLite in this process; a live agent in
//! ANOTHER process is fine (CONCURRENCY.md rule 9: separate processes are
//! serialised by the kernel; the rule forbids a second SQLite *library in
//! one process*, which this is not).
//!
//! Zero model calls: search embeds the query with the bundled embedder the
//! store already uses.

use std::error::Error;
use std::time::Duration;

use ratatui::crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Wrap};
use ratatui::Frame;
use yantrikdb::YantrikDB;

const PAGE_SIZE: usize = 200;
const SEARCH_TOP_K: usize = 50;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Pane {
    Namespaces,
    Memories,
    Inspector,
}

struct MemoryRow {
    rid: String,
    text: String,
    memory_type: String,
    importance: f64,
    created_at: f64,
    /// `Some(score)` for a search hit, `None` for a listing row.
    score: Option<f64>,
}

struct Inspector {
    rid: String,
    header: Vec<Line<'static>>,
    text: String,
    entities: Vec<String>,
    claims: Vec<String>,
    revisions: Vec<String>,
}

struct App {
    db: YantrikDB,
    store: String,
    namespaces: Vec<(String, i64)>,
    ns_state: ListState,
    memories: Vec<MemoryRow>,
    mem_state: ListState,
    total: usize,
    page: usize,
    query: String,
    typing: bool,
    /// The query the current memory list came from, if it is a search.
    active_query: Option<String>,
    inspector: Option<Inspector>,
    inspector_scroll: u16,
    tasks: Vec<String>,
    pane: Pane,
    status: String,
}

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = std::env::args().skip(1);
    let store = match args.next() {
        Some(p) if p != "--help" && p != "-h" => p,
        _ => {
            eprintln!("usage: yantrikdb-tui <store.db>\n\n\
                       Read-only terminal explorer for one YantrikDB store.\n\
                       keys: Tab/Shift-Tab panes · ↑/↓ or j/k move · Enter open · / search · Esc clear · \
                       n/p page · PgUp/PgDn scroll · r refresh · q quit");
            std::process::exit(2);
        }
    };
    if !std::path::Path::new(&store).is_file() {
        eprintln!("no store file at {store}");
        std::process::exit(2);
    }
    let db = YantrikDB::with_default(&store)?;
    let mut app = App::new(db, store)?;

    let mut terminal = ratatui::init();
    let result = run(&mut terminal, &mut app);
    ratatui::restore();
    result
}

fn run(terminal: &mut ratatui::DefaultTerminal, app: &mut App) -> Result<(), Box<dyn Error>> {
    loop {
        terminal.draw(|f| ui(f, app))?;
        if !event::poll(Duration::from_millis(250))? {
            continue;
        }
        if let Event::Key(key) = event::read()? {
            if key.kind != KeyEventKind::Press {
                continue;
            }
            if app.typing {
                match key.code {
                    KeyCode::Esc => {
                        app.typing = false;
                        app.query.clear();
                        app.active_query = None;
                        app.load_page(0);
                    }
                    KeyCode::Enter => {
                        app.typing = false;
                        app.search();
                    }
                    KeyCode::Backspace => {
                        app.query.pop();
                    }
                    KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        app.query.push(c);
                    }
                    _ => {}
                }
                continue;
            }
            match key.code {
                KeyCode::Char('q') => return Ok(()),
                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => return Ok(()),
                KeyCode::Tab => app.pane = next_pane(app.pane, true),
                KeyCode::BackTab => app.pane = next_pane(app.pane, false),
                KeyCode::Char('/') => {
                    app.typing = true;
                    app.pane = Pane::Memories;
                }
                KeyCode::Esc => {
                    if app.active_query.is_some() {
                        app.query.clear();
                        app.active_query = None;
                        app.load_page(0);
                    }
                }
                KeyCode::Char('r') => app.refresh(),
                KeyCode::Char('n') => {
                    if app.active_query.is_none() && (app.page + 1) * PAGE_SIZE < app.total {
                        app.load_page(app.page + 1);
                    }
                }
                KeyCode::Char('p') => {
                    if app.active_query.is_none() && app.page > 0 {
                        app.load_page(app.page - 1);
                    }
                }
                KeyCode::Down | KeyCode::Char('j') => app.move_selection(1),
                KeyCode::Up | KeyCode::Char('k') => app.move_selection(-1),
                KeyCode::PageDown => app.inspector_scroll = app.inspector_scroll.saturating_add(10),
                KeyCode::PageUp => app.inspector_scroll = app.inspector_scroll.saturating_sub(10),
                KeyCode::Enter => app.activate(),
                _ => {}
            }
        }
    }
}

fn next_pane(p: Pane, forward: bool) -> Pane {
    match (p, forward) {
        (Pane::Namespaces, true) | (Pane::Inspector, false) => Pane::Memories,
        (Pane::Memories, true) | (Pane::Namespaces, false) => Pane::Inspector,
        (Pane::Inspector, true) | (Pane::Memories, false) => Pane::Namespaces,
    }
}

impl App {
    fn new(db: YantrikDB, store: String) -> Result<Self, Box<dyn Error>> {
        let mut app = App {
            db,
            store,
            namespaces: Vec::new(),
            ns_state: ListState::default(),
            memories: Vec::new(),
            mem_state: ListState::default(),
            total: 0,
            page: 0,
            query: String::new(),
            typing: false,
            active_query: None,
            inspector: None,
            inspector_scroll: 0,
            tasks: Vec::new(),
            pane: Pane::Memories,
            status: String::new(),
        };
        app.load_namespaces()?;
        app.ns_state.select(Some(0));
        app.load_page(0);
        app.load_tasks();
        Ok(app)
    }

    /// "All" first, then every namespace with a live (non-tombstoned)
    /// memory count. Read through the engine's own connection: same
    /// library, same process, no second SQLite.
    fn load_namespaces(&mut self) -> Result<(), Box<dyn Error>> {
        let mut rows: Vec<(String, i64)> = Vec::new();
        {
            let conn = self.db.conn();
            let mut stmt = conn.prepare(
                "SELECT namespace, COUNT(*) FROM memories \
                 WHERE COALESCE(consolidation_status, 'active') != 'tombstoned' \
                 GROUP BY namespace ORDER BY namespace",
            )?;
            let it = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?;
            for row in it {
                rows.push(row?);
            }
        }
        let all: i64 = rows.iter().map(|(_, n)| n).sum();
        self.namespaces = std::iter::once(("(all)".to_string(), all)).chain(rows).collect();
        Ok(())
    }

    fn selected_namespace(&self) -> Option<String> {
        let i = self.ns_state.selected().unwrap_or(0);
        if i == 0 {
            None
        } else {
            self.namespaces.get(i).map(|(n, _)| n.clone())
        }
    }

    fn load_page(&mut self, page: usize) {
        let ns = self.selected_namespace();
        match self
            .db
            .list_memories(PAGE_SIZE, page * PAGE_SIZE, None, None, ns.as_deref(), "created_at")
        {
            Ok((mems, total)) => {
                self.memories = mems
                    .into_iter()
                    .map(|m| MemoryRow {
                        rid: m.rid,
                        text: m.text,
                        memory_type: m.memory_type,
                        importance: m.importance,
                        created_at: m.created_at,
                        score: None,
                    })
                    .collect();
                self.total = total;
                self.page = page;
                self.active_query = None;
                self.mem_state.select(if self.memories.is_empty() { None } else { Some(0) });
                self.status = format!(
                    "{} memories · page {}/{}",
                    total,
                    page + 1,
                    total.div_ceil(PAGE_SIZE).max(1)
                );
            }
            Err(e) => self.status = format!("list failed: {e}"),
        }
        self.open_selected();
    }

    /// Semantic search over the selected namespace, never reinforcing what
    /// it touches (`skip_reinforce = true`), never expanding entities.
    fn search(&mut self) {
        let q = self.query.trim().to_string();
        if q.is_empty() {
            self.load_page(0);
            return;
        }
        let ns = self.selected_namespace();
        let embedding = match self.db.embed(&q) {
            Ok(e) => e,
            Err(e) => {
                self.status = format!("embed failed: {e}");
                return;
            }
        };
        match self.db.recall(
            &embedding,
            SEARCH_TOP_K,
            None,
            None,
            false,
            false,
            Some(&q),
            true,
            ns.as_deref(),
            None,
            None,
            None,
            None,
            false,
            None,
            None,
        ) {
            Ok(hits) => {
                self.memories = hits
                    .into_iter()
                    .map(|h| MemoryRow {
                        rid: h.rid,
                        text: h.text,
                        memory_type: h.memory_type,
                        importance: h.importance,
                        created_at: h.created_at,
                        score: Some(h.score),
                    })
                    .collect();
                self.status = format!("{} hits for \"{}\" (Esc clears)", self.memories.len(), q);
                self.active_query = Some(q);
                self.mem_state.select(if self.memories.is_empty() { None } else { Some(0) });
                self.open_selected();
            }
            Err(e) => self.status = format!("search failed: {e}"),
        }
    }

    fn load_tasks(&mut self) {
        let Some(ns) = self.selected_namespace() else {
            self.tasks.clear();
            return;
        };
        self.tasks = match self.db.task_list(&ns, None) {
            Ok(ts) => ts
                .into_iter()
                .map(|t| format!("[{}] {} · {}", t.status, t.title, t.priority))
                .collect(),
            Err(e) => vec![format!("tasks unavailable: {e}")],
        };
    }

    fn refresh(&mut self) {
        let keep_ns = self.ns_state.selected();
        if let Err(e) = self.load_namespaces() {
            self.status = format!("refresh failed: {e}");
        }
        self.ns_state
            .select(keep_ns.filter(|i| *i < self.namespaces.len()).or(Some(0)));
        if self.active_query.is_some() {
            self.search();
        } else {
            self.load_page(self.page.min(self.total.div_ceil(PAGE_SIZE).saturating_sub(1)));
        }
        self.load_tasks();
    }

    fn move_selection(&mut self, delta: i32) {
        match self.pane {
            Pane::Namespaces => {
                let n = self.namespaces.len();
                if n == 0 {
                    return;
                }
                let i = self.ns_state.selected().unwrap_or(0) as i32 + delta;
                self.ns_state.select(Some(i.clamp(0, n as i32 - 1) as usize));
            }
            Pane::Memories => {
                let n = self.memories.len();
                if n == 0 {
                    return;
                }
                let i = self.mem_state.selected().unwrap_or(0) as i32 + delta;
                self.mem_state.select(Some(i.clamp(0, n as i32 - 1) as usize));
                self.open_selected();
            }
            Pane::Inspector => {
                self.inspector_scroll = if delta > 0 {
                    self.inspector_scroll.saturating_add(1)
                } else {
                    self.inspector_scroll.saturating_sub(1)
                };
            }
        }
    }

    fn activate(&mut self) {
        match self.pane {
            Pane::Namespaces => {
                self.query.clear();
                self.load_page(0);
                self.load_tasks();
                self.pane = Pane::Memories;
            }
            Pane::Memories => {
                self.open_selected();
                self.pane = Pane::Inspector;
            }
            Pane::Inspector => {}
        }
    }

    fn open_selected(&mut self) {
        self.inspector_scroll = 0;
        let Some(i) = self.mem_state.selected() else {
            self.inspector = None;
            return;
        };
        let Some(row) = self.memories.get(i) else {
            self.inspector = None;
            return;
        };
        let rid = row.rid.clone();
        self.inspector = Some(self.inspect(&rid));
    }

    /// Everything the store holds about one memory, via read-only engine
    /// APIs: the record, its entity links, the claims it backs and its
    /// revision history.
    fn inspect(&self, rid: &str) -> Inspector {
        let dim = Style::default().fg(Color::DarkGray);
        let key = Style::default().fg(Color::Cyan);
        let mut header = Vec::new();
        let mut text = String::new();
        match self.db.get(rid) {
            Ok(Some(m)) => {
                header.push(Line::from(vec![
                    Span::styled(m.memory_type.clone(), key),
                    Span::styled(" · ", dim),
                    Span::raw(m.namespace.clone()),
                    Span::styled(" · importance ", dim),
                    Span::raw(format!("{:.2}", m.importance)),
                    Span::styled(" · ", dim),
                    Span::raw(m.consolidation_status.clone()),
                ]));
                header.push(Line::from(vec![
                    Span::styled("created ", dim),
                    Span::raw(fmt_day(m.created_at)),
                    Span::styled("  domain ", dim),
                    Span::raw(m.domain.clone()),
                    Span::styled("  source ", dim),
                    Span::raw(m.source.clone()),
                ]));
                header.push(Line::from(vec![Span::styled(rid.to_string(), dim)]));
                if !m.metadata.is_null() && m.metadata != serde_json::json!({}) {
                    header.push(Line::from(vec![
                        Span::styled("metadata ", dim),
                        Span::raw(m.metadata.to_string()),
                    ]));
                }
                text = m.text;
            }
            Ok(None) => header.push(Line::from(Span::styled("(record not found)", dim))),
            Err(e) => header.push(Line::from(Span::styled(format!("get failed: {e}"), dim))),
        }
        let entities = self.db.memory_entities(rid).unwrap_or_default();
        let claims = match self.db.claims_for_memory(rid) {
            Ok(cs) => cs
                .iter()
                .map(|c| {
                    let s = |k: &str| c.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let neg = c.get("polarity").and_then(|v| v.as_i64()) == Some(-1);
                    let window = match (
                        c.get("valid_from").and_then(|v| v.as_f64()),
                        c.get("valid_to").and_then(|v| v.as_f64()),
                    ) {
                        (Some(a), Some(b)) => format!(" [{} → {}]", fmt_day(a), fmt_day(b)),
                        (Some(a), None) => format!(" [from {}]", fmt_day(a)),
                        (None, Some(b)) => format!(" [until {}]", fmt_day(b)),
                        (None, None) => String::new(),
                    };
                    format!(
                        "{} —{} {} → {}  ({}, {}){}",
                        s("src"),
                        if neg { " NOT" } else { "" },
                        s("rel_type"),
                        s("dst"),
                        s("status_suggestion"),
                        s("extractor"),
                        window
                    )
                })
                .collect(),
            Err(e) => vec![format!("claims unavailable: {e}")],
        };
        let revisions = match self.db.revision_history(rid) {
            Ok(rs) => rs
                .iter()
                .map(|r| {
                    format!(
                        "r{} · {} · {}\n    was: {}",
                        r.revision_num,
                        fmt_day(r.applied_at),
                        r.reason,
                        r.prior_text
                    )
                })
                .collect(),
            Err(e) => vec![format!("revisions unavailable: {e}")],
        };
        Inspector {
            rid: rid.to_string(),
            header,
            text,
            entities,
            claims,
            revisions,
        }
    }
}

// ── drawing ─────────────────────────────────────────────────────────

fn ui(f: &mut Frame, app: &mut App) {
    let outer = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(5), Constraint::Length(1)])
        .split(f.area());
    draw_title(f, outer[0], app);
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(24),
            Constraint::Percentage(38),
            Constraint::Min(30),
        ])
        .split(outer[1]);
    draw_namespaces(f, cols[0], app);
    draw_memories(f, cols[1], app);
    draw_inspector(f, cols[2], app);
    draw_footer(f, outer[2], app);
}

fn border(app: &App, pane: Pane, title: String) -> Block<'static> {
    let style = if app.pane == pane {
        Style::default().fg(Color::Yellow)
    } else {
        Style::default().fg(Color::DarkGray)
    };
    Block::default().borders(Borders::ALL).border_style(style).title(title)
}

fn draw_title(f: &mut Frame, area: Rect, app: &App) {
    let line = Line::from(vec![
        Span::styled("yantrikdb", Style::default().add_modifier(Modifier::BOLD)),
        Span::raw("  "),
        Span::styled(app.store.clone(), Style::default().fg(Color::DarkGray)),
        Span::raw("  read-only · "),
        Span::styled(
            "Tab panes · ↑↓ move · Enter open · / search · Esc clear · n/p page · PgUp/PgDn scroll · r refresh · q quit",
            Style::default().fg(Color::DarkGray),
        ),
    ]);
    f.render_widget(Paragraph::new(line), area);
}

fn draw_namespaces(f: &mut Frame, area: Rect, app: &mut App) {
    let items: Vec<ListItem> = app
        .namespaces
        .iter()
        .map(|(name, n)| {
            ListItem::new(Line::from(vec![
                Span::raw(format!("{name:<14}")),
                Span::styled(format!("{n:>6}"), Style::default().fg(Color::DarkGray)),
            ]))
        })
        .collect();
    let list = List::new(items)
        .block(border(app, Pane::Namespaces, " namespaces ".into()))
        .highlight_style(Style::default().add_modifier(Modifier::REVERSED));
    f.render_stateful_widget(list, area, &mut app.ns_state);
}

fn draw_memories(f: &mut Frame, area: Rect, app: &mut App) {
    let width = area.width.saturating_sub(4) as usize;
    let items: Vec<ListItem> = app
        .memories
        .iter()
        .map(|m| {
            let lead = match m.score {
                Some(s) => format!("{s:.2} "),
                None => format!("{} ", fmt_day(m.created_at)),
            };
            let room = width.saturating_sub(lead.len() + 6);
            let text = first_line(&m.text, room);
            ListItem::new(Line::from(vec![
                Span::styled(lead, Style::default().fg(Color::DarkGray)),
                Span::raw(text),
                Span::styled(
                    format!(" {}{:.1}", &m.memory_type[..1.min(m.memory_type.len())], m.importance),
                    Style::default().fg(Color::DarkGray),
                ),
            ]))
        })
        .collect();
    let title = if app.typing {
        format!(" search: {}▏ ", app.query)
    } else if let Some(q) = &app.active_query {
        format!(" search: {q} ")
    } else {
        " memories · newest first ".to_string()
    };
    let list = List::new(items)
        .block(border(app, Pane::Memories, title))
        .highlight_style(Style::default().add_modifier(Modifier::REVERSED));
    f.render_stateful_widget(list, area, &mut app.mem_state);
}

fn draw_inspector(f: &mut Frame, area: Rect, app: &App) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(8), Constraint::Length(6)])
        .split(area);
    let dim = Style::default().fg(Color::DarkGray);
    let head = Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD);
    let mut lines: Vec<Line> = Vec::new();
    match &app.inspector {
        None => lines.push(Line::from(Span::styled("select a memory", dim))),
        Some(ins) => {
            lines.extend(ins.header.iter().cloned());
            lines.push(Line::from(""));
            for l in ins.text.lines() {
                lines.push(Line::from(l.to_string()));
            }
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("ENTITIES · {}", ins.entities.len()),
                head,
            )));
            lines.push(Line::from(if ins.entities.is_empty() {
                Span::styled("none linked", dim)
            } else {
                Span::raw(ins.entities.join(" · "))
            }));
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("CLAIMS BACKED BY THIS MEMORY · {}", ins.claims.len()),
                head,
            )));
            if ins.claims.is_empty() {
                lines.push(Line::from(Span::styled("none active", dim)));
            }
            for c in &ins.claims {
                lines.push(Line::from(format!("• {c}")));
            }
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("REVISION HISTORY · {}", ins.revisions.len()),
                head,
            )));
            if ins.revisions.is_empty() {
                lines.push(Line::from(Span::styled("never corrected", dim)));
            }
            for r in &ins.revisions {
                for (i, l) in r.lines().enumerate() {
                    lines.push(Line::from(if i == 0 { format!("• {l}") } else { l.to_string() }));
                }
            }
        }
    }
    let title = match &app.inspector {
        Some(ins) => format!(" memory {} ", &ins.rid[..8.min(ins.rid.len())]),
        None => " inspector ".to_string(),
    };
    let para = Paragraph::new(lines)
        .block(border(app, Pane::Inspector, title))
        .wrap(Wrap { trim: false })
        .scroll((app.inspector_scroll, 0));
    f.render_widget(para, rows[0]);

    let task_lines: Vec<Line> = if app.selected_namespace().is_none() {
        vec![Line::from(Span::styled("select a namespace to see its tasks", dim))]
    } else if app.tasks.is_empty() {
        vec![Line::from(Span::styled("no tasks", dim))]
    } else {
        app.tasks.iter().map(|t| Line::from(t.clone())).collect()
    };
    let tasks = Paragraph::new(task_lines)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::DarkGray))
                .title(format!(" tasks · {} ", app.tasks.len())),
        )
        .wrap(Wrap { trim: true });
    f.render_widget(tasks, rows[1]);
}

fn draw_footer(f: &mut Frame, area: Rect, app: &App) {
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            app.status.clone(),
            Style::default().fg(Color::DarkGray),
        ))),
        area,
    );
}

// ── helpers ─────────────────────────────────────────────────────────

fn first_line(text: &str, width: usize) -> String {
    let line = text.lines().next().unwrap_or("");
    let mut out: String = line.chars().take(width.max(1)).collect();
    if line.chars().count() > width {
        out.push('…');
    }
    out
}

/// `YYYY-MM-DD` from unix seconds, no calendar crate: days-to-civil per
/// Howard Hinnant's algorithm.
fn fmt_day(secs: f64) -> String {
    if !secs.is_finite() || secs <= 0.0 {
        return "—".to_string();
    }
    let days = (secs / 86_400.0).floor() as i64;
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fmt_day_matches_known_dates() {
        assert_eq!(fmt_day(0.0), "—");
        assert_eq!(fmt_day(86_400.0), "1970-01-02");
        assert_eq!(fmt_day(1_789_300_000.0), "2026-09-13");
    }

    #[test]
    fn first_line_truncates_with_ellipsis() {
        assert_eq!(first_line("hello world\nsecond", 5), "hello…");
        assert_eq!(first_line("short", 10), "short");
    }
}
