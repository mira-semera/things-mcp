import pytest
from things_mcp import server, url_scheme
from things_mcp.server import reorder_todos, add_heading


@pytest.fixture
def no_sleep(mocker):
    mocker.patch('things_mcp.server.time.sleep')


def _todos(*ids, heading=None):
    return [{'uuid': i, 'title': i, 'heading': heading} if heading else {'uuid': i, 'title': i} for i in ids]


@pytest.mark.asyncio
async def test_reorder_requires_container():
    assert "pass project_id or heading_id" in await reorder_todos(ids=['a'])


@pytest.mark.asyncio
async def test_reorder_rejects_foreign_ids(mocker, no_sleep):
    mocker.patch('things.todos', return_value=_todos('a', 'b'))
    result = await reorder_todos(ids=['a', 'zzz'], project_id='P')
    assert "not open to-dos" in result and "zzz" in result


@pytest.mark.asyncio
async def test_reorder_noop_when_already_ordered(mocker, no_sleep):
    mocker.patch('things.todos', return_value=_todos('a', 'b'))
    execute = mocker.patch('things_mcp.url_scheme.execute_url')
    assert "already matches" in await reorder_todos(ids=['a', 'b'], project_id='P')
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_cycles_from_first_misplaced_item(mocker, no_sleep):
    # a b c d -> a c b d : only c, b, d have to move (a is already first)
    orders = [_todos('a', 'b', 'c', 'd'), _todos('a', 'c', 'b', 'd')]
    mocker.patch('things.todos', side_effect=lambda **kw: orders.pop(0) if len(orders) > 1 else orders[0])
    mocker.patch('things.projects', return_value=[{'uuid': 'PARK', 'title': server.REORDER_PARKING_TITLE}])
    update = mocker.spy(url_scheme, 'update_todo')
    mocker.patch('things_mcp.url_scheme.execute_url')
    result = await reorder_todos(ids=['a', 'c', 'b'], project_id='P')
    assert "verified" in result
    moved = [c.kwargs['id'] for c in update.call_args_list if c.kwargs.get('list_id') == 'PARK']
    assert moved == ['c', 'b', 'd']
    back = [c.kwargs for c in update.call_args_list if c.kwargs.get('list_id') == 'P']
    assert all(k.get('heading_id') is None for k in back)


@pytest.mark.asyncio
async def test_reorder_inside_heading_moves_back_under_heading(mocker, no_sleep):
    mocker.patch('things.get', return_value={'uuid': 'H', 'type': 'heading', 'project': 'P'})
    orders = [_todos('x', 'y', heading='H'), _todos('y', 'x', heading='H')]
    mocker.patch('things.todos', side_effect=lambda **kw: orders.pop(0) if len(orders) > 1 else orders[0])
    mocker.patch('things.projects', return_value=[{'uuid': 'PARK', 'title': server.REORDER_PARKING_TITLE}])
    update = mocker.spy(url_scheme, 'update_todo')
    mocker.patch('things_mcp.url_scheme.execute_url')
    assert "verified" in await reorder_todos(ids=['y'], heading_id='H')
    back = [c.kwargs for c in update.call_args_list if c.kwargs.get('list_id') == 'P']
    assert back and all(k['heading_id'] == 'H' for k in back)


@pytest.mark.asyncio
async def test_reorder_top_level_ignores_todos_under_headings(mocker, no_sleep):
    mocker.patch('things.todos', return_value=_todos('a') + _todos('h1', heading='H'))
    assert "already matches" in await reorder_todos(ids=['a'], project_id='P')


@pytest.mark.asyncio
async def test_add_heading_invalid_project(mocker):
    mocker.patch('things.get', return_value=None)
    assert "Invalid project UUID" in await add_heading(project_id='nope', title='X')


@pytest.mark.asyncio
async def test_add_heading_returns_new_id(mocker, no_sleep):
    mocker.patch('things.get', return_value={'uuid': 'P', 'type': 'project'})
    calls = [[{'uuid': 'old', 'title': 'Old'}],
             [{'uuid': 'old', 'title': 'Old'}, {'uuid': 'new', 'title': 'Nová'}]]
    mocker.patch('things.tasks', side_effect=lambda **kw: calls.pop(0) if len(calls) > 1 else calls[0])
    ui = mocker.patch('things_mcp.url_scheme.create_heading_via_ui')
    result = await add_heading(project_id='P', title='Nová')
    ui.assert_called_once_with(project_id='P', title='Nová')
    assert "id: new" in result


@pytest.mark.asyncio
async def test_add_heading_reports_missing_heading(mocker, no_sleep):
    mocker.patch('things.get', return_value={'uuid': 'P', 'type': 'project'})
    mocker.patch('things.tasks', return_value=[])
    mocker.patch('things_mcp.url_scheme.create_heading_via_ui')
    assert "no new heading appeared" in await add_heading(project_id='P', title='X')


def test_create_heading_escapes_title(mocker):
    run = mocker.patch('things_mcp.url_scheme.subprocess.run')
    url_scheme.create_heading_via_ui(project_id='P', title='a "b" \\ c')
    script = run.call_args.args[0][2]
    assert 'set value of f to "a \\"b\\" \\\\ c"' in script
    assert 'New Heading' in script
