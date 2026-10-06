"""Tests for reorder_todos and add_heading against a stateful fake of Things.

The fake applies each update URL the way Things 3.24 was observed to:
moving a to-do into a list appends it at the end, except for to-dos
scheduled for later/Someday (start == 2), which keep their index.
"""
import urllib.parse
import pytest
from things_mcp import server, url_scheme
from things_mcp.server import reorder_todos, add_heading


class FakeThings:
    def __init__(self, todos, drop_urls=0, fail_on=None, delay=0, raise_on=None):
        self.t = {}
        self.next_index = 0
        for i, (uuid, extra) in enumerate(todos):
            row = {'project': 'P', 'heading': None, 'start': 1, 'start_date': None,
                   'reminder': None, 'repeating': False, 'trashed': 0, 'status': 0, 'index': i}
            row.update(extra)
            self.t[uuid] = row
        self.next_index = len(todos)
        self.drop_urls = drop_urls      # ignore the first N URLs (Things too busy)
        self.fail_on = fail_on          # (uuid, list_id) that Things never applies
        self.urls = []
        self.delay = delay              # URLs take effect only after this many state reads
        self.raise_on = raise_on        # (uuid, list_id) whose URL raises when sent
        self.queue = []

    def execute_url(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if self.raise_on and self.raise_on == (q.get('id'), q.get('list-id')):
            raise RuntimeError("osascript failed")
        self.urls.append(url)
        if self.drop_urls:
            self.drop_urls -= 1
            return
        self.queue.append([self.delay, url])
        self._tick(0)

    def _tick(self, step=1):
        for item in self.queue:
            item[0] -= step
        while self.queue and self.queue[0][0] <= 0:
            self._apply(self.queue.pop(0)[1])

    def _apply(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        row = self.t[q['id']]
        if 'when' in q:
            if q['when'] == 'anytime':
                row['start'] = 1
            else:
                row['start'] = 2
                date = q['when'].split('@')[0]
                row['start_date'] = None if date == 'someday' else ('D:' + date)
                row['reminder'] = ('T:' + q['when'].split('@')[1]) if '@' in q['when'] else None
        if 'list-id' in q:
            if self.fail_on == (q['id'], q['list-id']):
                return
            heading = q.get('heading-id')
            row['project'] = None if heading else q['list-id']
            row['heading'] = heading
            if row['start'] != 2:
                row['index'] = self.next_index
                self.next_index += 1

    def state(self, uuid):
        self._tick()
        return dict(self.t[uuid]) if uuid in self.t else {}

    def todos(self, project=None, heading=None, status=None):
        rows = [(u, r) for u, r in self.t.items()
                if (heading and r['heading'] == heading) or (project and r['project'] == project)]
        rows.sort(key=lambda x: x[1]['index'])
        return [{'uuid': u, 'title': u, 'heading': r['heading'],
                 'start': 'Someday' if r['start'] == 2 else 'Anytime'} for u, r in rows]


@pytest.fixture
def fake(mocker, tmp_path, monkeypatch):
    def make(todos, **kw):
        f = FakeThings(todos, **kw)
        mocker.patch('things_mcp.server.time.sleep')
        monkeypatch.setattr(server, 'STEP_TIMEOUT', 0.05)
        monkeypatch.setattr(server, 'REORDER_STATE_DIR', str(tmp_path))
        mocker.patch('things_mcp.url_scheme.execute_url', side_effect=f.execute_url)
        mocker.patch('things_mcp.server._todo_state', side_effect=f.state)
        mocker.patch('things_mcp.server._container_has_repeating_templates', return_value=False)
        mocker.patch('things_mcp.server._get_or_create_parking_project', return_value='PARK')
        mocker.patch('things_mcp.server._parking_candidates', return_value=[{'uuid': 'PARK'}])
        mocker.patch('things.todos', side_effect=f.todos)
        mocker.patch('things.get', side_effect=lambda u: {'uuid': u, 'type': 'heading' if u == 'H' else 'project', 'project': 'P'})
        return f
    return make


def order(f, project='P', heading=None):
    return [t['uuid'] for t in f.todos(project=None if heading else project, heading=heading)]


@pytest.mark.asyncio
async def test_requires_container():
    assert "pass project_id or heading_id" in await reorder_todos(ids=['a'])


@pytest.mark.asyncio
async def test_rejects_foreign_ids(fake):
    fake([('a', {}), ('b', {})])
    result = await reorder_todos(ids=['a', 'zzz'], project_id='P')
    assert "not open to-dos" in result and "zzz" in result


@pytest.mark.asyncio
async def test_noop_when_already_ordered(fake):
    f = fake([('a', {}), ('b', {})])
    assert "already matches" in await reorder_todos(ids=['a', 'b'], project_id='P')
    assert f.urls == []


@pytest.mark.asyncio
async def test_reorders_and_only_moves_from_first_misplaced(fake):
    f = fake([('a', {}), ('b', {}), ('c', {}), ('d', {})])
    assert "verified" in await reorder_todos(ids=['a', 'c', 'b'], project_id='P')
    assert order(f) == ['a', 'c', 'b', 'd']
    assert not any('id=a&' in u or u.endswith('id=a') for u in f.urls)


@pytest.mark.asyncio
async def test_reorders_inside_heading(fake):
    f = fake([('x', {'project': None, 'heading': 'H'}), ('y', {'project': None, 'heading': 'H'})])
    assert "verified" in await reorder_todos(ids=['y'], heading_id='H')
    assert order(f, heading='H') == ['y', 'x']


@pytest.mark.asyncio
async def test_refuses_to_move_todos_with_a_start_date(fake):
    f = fake([('a', {}), ('today', {'start_date': 'D:2026-10-06'}), ('b', {})])
    result = await reorder_todos(ids=['b'], project_id='P')
    assert "start date" in result and "today" in result
    assert f.urls == []


@pytest.mark.asyncio
async def test_dated_todo_before_the_change_is_left_alone(fake):
    f = fake([('today', {'start_date': 'D:2026-10-06'}), ('a', {}), ('b', {})])
    assert "verified" in await reorder_todos(ids=['today', 'b', 'a'], project_id='P')
    assert order(f) == ['today', 'b', 'a']


@pytest.mark.asyncio
async def test_someday_todos_are_outside_the_order(fake):
    f = fake([('a', {}), ('later', {'start': 2}), ('b', {})])
    assert "verified" in await reorder_todos(ids=['b', 'a'], project_id='P')
    assert [u for u in order(f) if u != 'later'] == ['b', 'a']
    assert not any('id=later' in u for u in f.urls)


@pytest.mark.asyncio
async def test_dropped_url_is_not_resent_and_changes_nothing(fake):
    f = fake([('a', {}), ('b', {})], drop_urls=1)
    assert "Nothing changed" in await reorder_todos(ids=['b'], project_id='P')
    assert order(f) == ['a', 'b'] and len(f.urls) == 1


@pytest.mark.asyncio
async def test_move_that_never_applies_changes_nothing(fake):
    f = fake([('a', {}), ('b', {}), ('c', {})], fail_on=('b', 'PARK'))
    result = await reorder_todos(ids=['b', 'a'], project_id='P')
    assert "stopped at to-do b" in result and "Nothing changed" in result
    assert order(f) == ['a', 'b', 'c']
    assert not any('list-id=P&' in u or u.endswith('list-id=P') for u in f.urls)


@pytest.mark.asyncio
async def test_each_move_is_sent_once_even_when_late(fake):
    f = fake([('a', {}), ('b', {}), ('c', {})], delay=30)
    assert "verified" in await reorder_todos(ids=['c', 'a'], project_id='P')
    assert order(f) == ['c', 'a', 'b']
    assert len(f.urls) == 6  # three to-dos, out and back, no duplicates


@pytest.mark.asyncio
async def test_send_failure_on_return_is_reported(fake):
    f = fake([('a', {}), ('b', {})], raise_on=('b', 'P'))
    result = await reorder_todos(ids=['b'], project_id='P')
    assert "could not be moved back" in result and "parking project" in result


@pytest.mark.asyncio
async def test_send_failure_on_leave_changes_nothing(fake):
    f = fake([('a', {}), ('b', {})], raise_on=('b', 'PARK'))
    result = await reorder_todos(ids=['b'], project_id='P')
    assert "Nothing changed" in result and order(f) == ['a', 'b']


@pytest.mark.asyncio
async def test_rechecks_dates_under_the_lock(fake, mocker):
    f = fake([('a', {}), ('b', {})])
    real = server._get_or_create_parking_project
    def schedule_b_meanwhile():
        f.t['b']['start_date'] = 'D:2026-10-06'
        return 'PARK'
    mocker.patch('things_mcp.server._get_or_create_parking_project', side_effect=schedule_b_meanwhile)
    assert "start date" in await reorder_todos(ids=['b'], project_id='P')
    assert f.urls == []


def test_parking_adopts_existing_project_when_state_file_is_lost(mocker, tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'REORDER_STATE_DIR', str(tmp_path))
    mocker.patch('things.projects', return_value=[{'uuid': 'OLD', 'title': server.REORDER_PARKING_TITLE}])
    execute = mocker.patch('things_mcp.url_scheme.execute_url')
    assert server._get_or_create_parking_project() == 'OLD'
    execute.assert_not_called()
    import json, os
    assert json.load(open(os.path.join(str(tmp_path), 'parking.json')))['uuid'] == 'OLD'


@pytest.mark.asyncio
async def test_failed_return_is_reported_loudly(fake):
    f = fake([('a', {}), ('b', {})], fail_on=('b', 'P'))
    result = await reorder_todos(ids=['b'], project_id='P')
    assert "could not be moved back" in result


@pytest.mark.asyncio
async def test_refuses_when_parking_has_leftovers(fake, mocker):
    f = fake([('a', {}), ('b', {}), ('stuck', {'project': 'OLDPARK'})])
    mocker.patch('things_mcp.server._parking_candidates', return_value=[{'uuid': 'PARK'}, {'uuid': 'OLDPARK'}])
    result = await reorder_todos(ids=['b', 'a'], project_id='P')
    assert "interrupted run" in result and "stuck" in result
    assert f.urls == []


@pytest.mark.asyncio
async def test_refuses_repeating(fake):
    f = fake([('a', {}), ('b', {'repeating': True})])
    assert "repeating" in await reorder_todos(ids=['b', 'a'], project_id='P')
    assert f.urls == []


@pytest.mark.asyncio
async def test_refuses_list_with_repeating_templates(fake, mocker):
    f = fake([('a', {}), ('b', {})])
    mocker.patch('things_mcp.server._container_has_repeating_templates', return_value=True)
    assert "repeating" in await reorder_todos(ids=['b'], project_id='P')
    assert f.urls == []


@pytest.mark.asyncio
async def test_refuses_area(fake, mocker):
    fake([('a', {})])
    mocker.patch('things.get', return_value={'uuid': 'AREA', 'type': 'area'})
    assert "not a project" in await reorder_todos(ids=['a'], project_id='AREA')


@pytest.mark.asyncio
async def test_concurrent_run_is_refused(fake, tmp_path):
    import fcntl, os
    fake([('a', {}), ('b', {})])
    held = open(os.path.join(str(tmp_path), "mutation.lock"), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert "in progress" in await reorder_todos(ids=['b'], project_id='P')
    finally:
        held.close()


@pytest.mark.asyncio
async def test_add_heading_invalid_project(mocker):
    mocker.patch('things.get', return_value=None)
    assert "Invalid project UUID" in await add_heading(project_id='nope', title='X')


@pytest.fixture
def heading_env(mocker, tmp_path, monkeypatch):
    """Project P with heading 'old' holding t1, t2. Things stores a new heading once its title is typed."""
    mocker.patch('things_mcp.server.time.sleep')
    monkeypatch.setattr(server, 'REORDER_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(server, 'STEP_TIMEOUT', 0.05)
    env = {'headings': [{'uuid': 'old', 'title': 'Old', 'type': 'heading', 'index': 1}],
           'new_index': 5, 'todo_heading': {'t1': 'old', 't2': 'old'}, 'typed': None}
    mocker.patch('things.tasks', side_effect=lambda **kw: [dict(h) for h in env['headings']])
    mocker.patch('things.todos', side_effect=lambda **kw: [{'uuid': u, 'heading': h} for u, h in env['todo_heading'].items() if h == kw.get('heading')])
    def get(u):
        if u == 'P':
            return {'uuid': 'P', 'type': 'project', 'title': 'Proj'}
        for h in env['headings']:
            if h['uuid'] == u:
                return dict(h)
        return {'uuid': u, 'heading': env['todo_heading'].get(u)}
    mocker.patch('things.get', side_effect=get)
    def click(project_id, select_id, project_title):
        env['select_id'], env['project_title'] = select_id, project_title
        return 'ok'
    def fill(title, project_title=None):
        env['typed'] = title
        env['headings'].append({'uuid': 'new', 'title': title, 'type': 'heading', 'index': env['new_index']})
        return 'ok'
    env['click'] = mocker.patch('things_mcp.url_scheme.open_new_heading_field', side_effect=click)
    env['fill'] = mocker.patch('things_mcp.url_scheme.fill_focused_heading_field', side_effect=fill)
    env['cancel'] = mocker.patch('things_mcp.url_scheme.cancel_heading_field')
    return env


@pytest.mark.asyncio
async def test_add_heading_invalid_project(mocker):
    mocker.patch('things.get', return_value=None)
    assert "Invalid project UUID" in await add_heading(project_id='nope', title='X')


@pytest.mark.asyncio
async def test_add_heading_selects_last_todo_and_returns_id(heading_env):
    result = await add_heading(project_id='P', title='Nová')
    assert heading_env['select_id'] == 't2' and heading_env['project_title'] == 'Proj'
    assert result == "Created heading: Nová (id: new)"


@pytest.mark.asyncio
async def test_add_heading_types_nothing_when_no_field_gets_focus(heading_env):
    heading_env['click'].side_effect = lambda **kw: 'no empty heading field got focus'
    result = await add_heading(project_id='P', title='X')
    assert result.startswith("Error: nothing was created") and heading_env['typed'] is None


@pytest.mark.asyncio
async def test_add_heading_aborted_before_click(heading_env):
    heading_env['click'].side_effect = lambda **kw: 'Things did not select the expected item in the project'
    result = await add_heading(project_id='P', title='X')
    assert result.startswith("Error: nothing was created") and heading_env['typed'] is None


@pytest.mark.asyncio
async def test_add_heading_discards_when_focus_moved_before_typing(heading_env):
    heading_env['fill'].side_effect = lambda title, project_title=None: 'the focused element is not an empty text field'
    result = await add_heading(project_id='P', title='X')
    assert "discarded" in result
    heading_env['cancel'].assert_called_once()


@pytest.mark.asyncio
async def test_add_heading_warns_when_not_last_or_todos_moved(heading_env):
    heading_env['new_index'] = 0
    def fill(title, project_title=None):
        heading_env['headings'].append({'uuid': 'new', 'title': title, 'type': 'heading', 'index': 0})
        heading_env['todo_heading']['t2'] = 'new'
        return 'ok'
    heading_env['fill'].side_effect = fill
    result = await add_heading(project_id='P', title='X')
    assert result.startswith("Warning") and "not the last heading" in result and "t2" in result


@pytest.mark.asyncio
async def test_add_heading_refuses_concurrent_run(heading_env, tmp_path):
    import fcntl, os
    held = open(os.path.join(str(tmp_path), "mutation.lock"), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert "in progress" in await add_heading(project_id='P', title='X')
    finally:
        held.close()


def test_ui_step_one_refuses_on_locked_screen(mocker):
    mocker.patch('things_mcp.url_scheme._screen_is_locked', return_value=True)
    run = mocker.patch('things_mcp.url_scheme.subprocess.run')
    assert "screen is locked" in url_scheme.open_new_heading_field(project_id='P')
    run.assert_not_called()


def test_ui_scripts_check_window_selection_menu_and_focus(mocker):
    mocker.patch('things_mcp.url_scheme._screen_is_locked', return_value=False)
    run = mocker.patch('things_mcp.url_scheme.subprocess.run')
    run.return_value.stdout = 'cmux|ok\n'
    assert url_scheme.open_new_heading_field(project_id='P"x', project_title='Můj "p"') == 'ok'
    script = [c.args[0][2] for c in run.call_args_list if 'New Heading' in c.args[0][2]][0]
    assert 'id of item 1 of sel is "P\\"x"' in script
    assert 'name of front window is not "Můj \\"p\\""' in script
    assert 'enabled of mi' in script and '"AXTextField"' in script
    assert 'set value of f' not in script  # step 1 types nothing
    run.return_value.stdout = 'ok\n'
    assert url_scheme.fill_focused_heading_field('a "b" \\ c') == 'ok'
    script = [c.args[0][2] for c in run.call_args_list if 'set value of f' in c.args[0][2]][0]
    assert 'set value of f to "a \\"b\\" \\\\ c"' in script
    assert 'frontmost is true is not "Things3"' in script


def test_ui_step_one_escapes_when_click_gave_no_field(mocker):
    mocker.patch('things_mcp.url_scheme._screen_is_locked', return_value=False)
    run = mocker.patch('things_mcp.url_scheme.subprocess.run')
    run.return_value.stdout = 'cmux|clicked|no empty heading field got focus\n'
    assert url_scheme.open_new_heading_field(project_id='P') == 'no empty heading field got focus'
    assert any('key code 53' in c.args[0][2] for c in run.call_args_list)


@pytest.mark.asyncio
async def test_add_heading_checks_top_level_todos_too(heading_env):
    heading_env['headings'].clear()
    heading_env['todo_heading'] = {'top1': None}
    def todos(**kw):
        if kw.get('project') == 'P':
            return [{'uuid': u, 'heading': h} for u, h in heading_env['todo_heading'].items()]
        return [{'uuid': u, 'heading': h} for u, h in heading_env['todo_heading'].items() if h == kw.get('heading')]
    import things
    things.todos.side_effect = todos
    def fill(title, project_title=None):
        heading_env['headings'].append({'uuid': 'new', 'title': title, 'type': 'heading', 'index': 0})
        heading_env['todo_heading']['top1'] = 'new'  # Things swallowed the top-level to-do
        return 'ok'
    heading_env['fill'].side_effect = fill
    result = await add_heading(project_id='P', title='X')
    assert result.startswith("Warning") and "top1" in result


def test_ui_step_one_cleans_up_when_script_fails(mocker):
    import subprocess
    mocker.patch('things_mcp.url_scheme._screen_is_locked', return_value=False)
    cancel = mocker.patch('things_mcp.url_scheme.cancel_heading_field')
    mocker.patch('things_mcp.url_scheme.subprocess.run',
                 side_effect=subprocess.CalledProcessError(1, 'osascript', stderr='boom'))
    assert "discarded" in url_scheme.open_new_heading_field(project_id='P')
    cancel.assert_called_once()


def test_ui_step_two_rechecks_project_window(mocker):
    run = mocker.patch('things_mcp.url_scheme.subprocess.run')
    run.return_value.stdout = 'ok\n'
    url_scheme.fill_focused_heading_field('t', project_title='Proj')
    script = [c.args[0][2] for c in run.call_args_list if 'set value of f' in c.args[0][2]][0]
    assert 'name of front window is not "Proj"' in script
