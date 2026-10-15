"""Main question materials and complete retained Files through the real SPA.

Readers, explicit downloads, keyboard navigation and reloads use the production
server. Evidence is retained via OUROBOROS_BROWSER_EVIDENCE_OUT, including RED.
"""

import hashlib
import struct
import zlib
import json
import os
from pathlib import Path

import pytest

from devtools.benchmarks.common.server_runner import _api
from tests.test_owner_wait_integration import wait_clone as clone_fixture
from tests.system_e2e.harness import KeylessIsolatedServer, ScriptedStubModel, keyless_settings, write_settings_file

wait_clone = clone_fixture
pytestmark = [pytest.mark.serial, pytest.mark.browser]



def _m1660_png():
    """A visible PNG fixture, made without an optional imaging dependency."""
    width, height = 240, 120
    def chunk(kind, value):
        return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value) & 0xffffffff)
    rows = b''.join(b'\x00' + (b'\x17\x39\x66' if y < 60 else b'\xf4\xa2\x43') * width for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def _m1660_material_question(root):
    from ouroboros.artifacts import store_task_artifact_bytes
    from ouroboros.owner_quiz import mark_published, quiz_states, reconcile_terminal, record_publication
    from ouroboros.project_dialogue import project_question_pointer
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.core_artifacts import _escalate
    from supervisor.message_bus import log_chat
    from tests.test_quiz_answer import _tool_ctx

    project = create_project(root, 'material-review', name='Material review')
    task_id = 'material-review-task'
    bind_task_to_project(root, task_id, project['id'], project['chat_id'], origin={'absent': 'system'})
    payloads = {'comparison.png': _m1660_png(),
                'decision-brief.md': b'# Decision brief\n\nChoose the amber panel for the printed edition.\n',
                'supporting-notes.txt': b'The measurements were captured before the decision.\n'}
    for name, payload in payloads.items():
        store_task_artifact_bytes(root, task_id, name=name, data=payload)
    ctx = _tool_ctx(root, task_id=task_id, chat_id=project['chat_id'])
    ctx.task_metadata['root_task_id'] = task_id
    receipt = _escalate(ctx, question='Which panel should the next edition use?',
                       options=[{'label': 'Amber panel', 'detail': 'Use the printed comparison.'},
                                {'label': 'Blue panel', 'detail': 'Keep the existing edition.'}],
                       assumption='Keep the current edition until you choose.',
                       materials=[{'name': name} for name in payloads])
    assert receipt.startswith('OK:'), receipt
    [quiz] = quiz_states(root, task_id).values()
    quiz_id = quiz['quiz_id']
    record_publication(root, task_id, quiz_id, project['chat_id'])
    write_task_result(root, task_id, 'completed', project_id=project['id'], chat_id=project['chat_id'],
                      result='The evidence is ready for your choice.')
    reconcile_terminal(root, task_id)
    block = quiz_states(root, task_id)[quiz_id]
    log_chat('out', project['chat_id'], 1, block['question'], task_id=task_id, record_type='quiz',
             quiz=block, drive_root=root, require_write=True)
    mark_published(root, task_id, quiz_id)
    block = quiz_states(root, task_id)[quiz_id]
    pointer = project_question_pointer({'task_id': task_id, 'quiz_id': quiz_id}, block, project)
    log_chat('out', 1, 1, pointer['text'], task_id=task_id, message_meta=pointer, drive_root=root, require_write=True)
    return {'project': project, 'task_id': task_id, 'quiz_id': quiz_id, 'materials': block['materials'],
            'escalate_receipt': receipt}, payloads


def _m1660_material_evidence(tmp_path, stem):
    evidence = Path(os.environ.get('OUROBOROS_BROWSER_EVIDENCE_OUT') or tmp_path / 'screenshots') / stem
    evidence.mkdir(parents=True, exist_ok=True)
    return evidence


def _m1660_material_finish(page, server, evidence, receipt):
    """Retain the actual failing screen and authoritative state before shutdown."""
    try:
        receipt['state_after'] = _api(server.base_url, 'GET', '/api/state')
        receipt['task_after'] = _api(server.base_url, 'GET', f"/api/tasks/{receipt['task_id']}")
    except Exception as exc:
        receipt['evidence_api_error'] = str(exc)
    if page is not None:
        try:
            page.screenshot(path=str(evidence / 'end.png'), full_page=True, animations='disabled')
            receipt['url_after'] = page.url
            receipt['body_after'] = page.locator('body').inner_text()
        except Exception as exc:
            receipt['evidence_browser_error'] = str(exc)
    (evidence / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')


def _m1660_tab_to(page, target, limit=80):
    """Real keyboard traversal; evaluating activeElement only observes focus."""
    for _ in range(limit):
        page.keyboard.press('Tab')
        if target.evaluate('(node) => node === document.activeElement'):
            return
    pytest.fail(f'Keyboard traversal did not reach {target}')


@pytest.mark.parametrize('width', [1100, 390], ids=['desktop', 'narrow'])
def test_main_question_materials_reader_and_keyboard(wait_clone, tmp_path, width):
    from playwright.sync_api import expect, sync_playwright

    root = tmp_path / 'instance' / 'data'
    root.mkdir(parents=True)
    receipt, payloads = _m1660_material_question(root)
    evidence = _m1660_material_evidence(tmp_path, f'materials-{width}')
    receipt.update(viewport={'width': width, 'height': 900}, stages=[], responses=[])
    with ScriptedStubModel([{'final': 'Unused model fixture.'}]) as stub:
        settings_path = root / 'settings.json'
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport=receipt['viewport'], reduced_motion='reduce')
                try:
                    page.goto(server.base_url, wait_until='domcontentloaded')
                    # The Project question's Main copy stands in the feed at its ask.
                    card = page.locator(f'#chat-messages .project-question-card[data-quiz-id="{receipt["quiz_id"]}"]')
                    card.wait_for(timeout=30000)
                    materials = card.locator('.chat-quiz-materials')
                    expect(materials.locator('.chat-file-card')).to_have_count(3)
                    image = materials.locator('img.chat-file-thumb')
                    expect(image).to_be_visible()
                    page.wait_for_function('(img) => img.complete && img.naturalWidth === 240 && img.naturalHeight === 120', arg=image.element_handle())
                    for material in receipt['materials']:
                        response = page.request.get(server.base_url + material['download_url'])
                        actual = response.body()
                        receipt['responses'].append({'name': material['label'], 'status': response.status,
                            'sha256': hashlib.sha256(actual).hexdigest(), 'content_type': response.headers.get('content-type')})
                        assert response.status == 200
                        assert actual == payloads[material['label']]
                        assert hashlib.sha256(actual).hexdigest() == material['sha256']
                    receipt['stages'].append('PNG decoded at 240x120; all material bytes match registered custody')
                    document = materials.get_by_role('button', name='decision-brief.md', exact=False)
                    # The keyboard starts at the question text, where Main's waiting pill lands a question.
                    card.locator('.chat-quiz-question').focus()
                    _m1660_tab_to(page, document)
                    expect(document).to_be_focused()
                    page.keyboard.press('Enter')
                    dialog = page.locator('dialog.document-reader')
                    expect(dialog).to_be_visible()
                    dialog.get_by_role('heading', name='Decision brief', exact=True).wait_for()
                    dialog.get_by_text('Choose the amber panel for the printed edition.', exact=True).wait_for()
                    expect(dialog.get_by_role('region', name='Document text')).to_be_focused()
                    page.screenshot(path=str(evidence / 'document-reader.png'), animations='disabled')
                    dialog.get_by_role('button', name='Source', exact=True).click()
                    expect(dialog.locator('.document-reader-body')).to_contain_text('# Decision brief')
                    page.keyboard.press('Escape')
                    expect(dialog).to_have_count(0)
                    expect(document).to_be_focused()
                    receipt['stages'].append('Keyboard opened existing reader; Markdown and source readable; Escape restored focus')
                    option = card.get_by_role('button', name='Amber panel Use the printed comparison.', exact=True)
                    _m1660_tab_to(page, option)
                    expect(option).to_be_focused()
                    overflow = page.evaluate('({width: innerWidth, scroll: document.documentElement.scrollWidth})')
                    receipt['layout'] = overflow
                    assert overflow['scroll'] <= overflow['width'] + 1, overflow
                    assert '#project/' not in page.url, 'Materials must be usable with the Project closed'
                    page.screenshot(path=str(evidence / 'question-focused.png'), animations='disabled')
                    receipt['stages'].append('Keyboard reached answer control; Main remains selected; no page horizontal overflow')
                finally:
                    _m1660_material_finish(page, server, evidence, receipt)
                    browser.close()
        finally:
            server.stop()


def test_main_question_unavailable_material_is_explicit(wait_clone, tmp_path):
    from playwright.sync_api import expect, sync_playwright
    from ouroboros.artifacts import task_artifact_dir_path

    root = tmp_path / 'instance' / 'data'
    root.mkdir(parents=True)
    receipt, _payloads = _m1660_material_question(root)
    evidence = _m1660_material_evidence(tmp_path, 'material-unavailable')
    material = next(row for row in receipt['materials'] if row['label'] == 'supporting-notes.txt')
    # Real loss of a retained fixture file; no intercepted network or DOM state.
    missing = task_artifact_dir_path(root, receipt['task_id']) / material['name']
    missing.unlink()
    receipt.update(removed_fixture=str(missing), stages=[])
    with ScriptedStubModel([{'final': 'Unused model fixture.'}]) as stub:
        settings_path = root / 'settings.json'
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={'width': 1100, 'height': 900}, reduced_motion='reduce')
                try:
                    page.goto(server.base_url, wait_until='domcontentloaded')
                    card = page.locator(f'#chat-messages .project-question-card[data-quiz-id="{receipt["quiz_id"]}"]')
                    card.wait_for(timeout=30000)
                    with page.expect_response(lambda response: response.url.endswith(material['download_url'])) as read:
                        card.get_by_role('button', name='supporting-notes.txt', exact=False).click()
                    receipt['response_status'] = read.value.status
                    assert read.value.status == 404
                    dialog = page.locator('dialog.document-reader')
                    expect(dialog).to_be_visible()
                    dialog.get_by_text('This delivered copy is missing or did not pass its integrity check, so it is not shown. Ask Ouroboros to send the file again.', exact=True).wait_for()
                    expect(dialog).not_to_contain_text('The measurements were captured before the decision.')
                    assert not dialog.get_by_role('button', name='Retry', exact=True).count()
                    receipt['stages'].append('Real missing artifact returned 404 and reader names unavailable copy')
                    page.screenshot(path=str(evidence / 'unavailable-reader.png'), animations='disabled')
                    page.keyboard.press('Escape')
                    expect(card.get_by_role('button', name='Amber panel Use the printed comparison.', exact=True)).to_be_enabled()
                    assert '#project/' not in page.url
                finally:
                    _m1660_material_finish(page, server, evidence, receipt)
                    browser.close()
        finally:
            server.stop()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_main_files_inventory_includes_more_than_eight_files(wait_clone, tmp_path, engine):
    from playwright.sync_api import expect, sync_playwright
    from ouroboros.artifacts import collect_task_artifact_records, store_task_artifact_bytes
    from ouroboros.project_dialogue import project_completion_delivery_outcome
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.task_results import write_task_result
    from supervisor.message_bus import log_chat

    root = tmp_path / 'instance' / 'data'
    root.mkdir(parents=True)
    task_id = 'eleven-files-task'
    project = create_project(root, 'retained-files', name='Retained files')
    bind_task_to_project(root, task_id, project['id'], project['chat_id'], origin={'absent': 'system'})
    payloads = {f'part-{index:02d}.txt': f'Retained result number {index}.\n'.encode() for index in range(1, 12)}
    for name, payload in payloads.items():
        store_task_artifact_bytes(root, task_id, name=name, data=payload)
    records = collect_task_artifact_records(root, task_id, require_registered=True)
    result = write_task_result(root, task_id, 'completed', project_id=project['id'], chat_id=project['chat_id'],
        terminal_origin='model_final', result='All eleven retained reports are available.', artifacts=records,
        outcome_axes={'execution': {'status': 'ok'}})
    log_chat('out', project['chat_id'], 1, result['result'], task_id=task_id, drive_root=root, require_write=True)
    task = {'id': task_id, 'project_id': project['id'], 'chat_id': project['chat_id'], 'root_task_id': task_id}
    disposition, _queued = project_completion_delivery_outcome(root, {}, task_id, task, result, {'status': 'completed'})
    assert disposition == 'owed'
    evidence = _m1660_material_evidence(tmp_path, f'files-eleven-{engine}')
    receipt = {'engine': engine, 'task_id': task_id, 'project': project, 'producer_disposition': disposition,
               'registered_artifacts': records, 'stages': []}
    with ScriptedStubModel([{'final': 'Unused model fixture.'}]) as stub:
        settings_path = root / 'settings.json'
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={'width': 1100, 'height': 900}, reduced_motion='reduce')
                downloads = []
                page.on('download', lambda download: downloads.append(download))
                try:
                    page.goto(server.base_url, wait_until='domcontentloaded')
                    final = page.locator('.chat-bubble.project-answer').filter(has_text=result['result'])
                    final.wait_for(timeout=30000)
                    files = final.get_by_role('button', name='Files', exact=True)
                    files.click()
                    inventory = final.locator('.project-answer-files')
                    expect(inventory.get_by_role('button')).to_have_count(11)
                    assert 'more files' not in inventory.inner_text()
                    for name in payloads:
                        entry = inventory.locator(f'[data-result-file="{name}"]')
                        expect(entry.get_by_text(name, exact=True)).to_be_visible()
                        expect(entry.get_by_role('button')).to_be_enabled()
                    page.screenshot(path=str(evidence / 'complete-inventory.png'), animations='disabled')
                    last = inventory.locator('[data-result-file="part-11.txt"]').get_by_role('button')
                    last.click()
                    dialog = page.locator('dialog.document-reader')
                    expect(dialog).to_be_visible()
                    expect(dialog.locator('.document-reader-body')).to_contain_text(payloads['part-11.txt'].decode().strip())
                    assert downloads == [], 'Opening a retained file must read it without downloading'
                    page.screenshot(path=str(evidence / 'eleventh-file-reader.png'), animations='disabled')
                    with page.expect_download() as download_info:
                        dialog.get_by_role('button', name='Download', exact=True).click()
                    download = download_info.value
                    assert downloads == [download], 'Only the explicit Download action may download the file'
                    assert download.suggested_filename == 'part-11.txt'
                    download_path = evidence / 'downloaded-part-11.txt'
                    download.save_as(str(download_path))
                    assert download_path.read_bytes() == payloads['part-11.txt']
                    receipt['download_sha256'] = hashlib.sha256(download_path.read_bytes()).hexdigest()
                    receipt['stages'].append('All eleven files visible; eleventh read in Main and explicitly downloaded with exact retained bytes')
                    dialog.get_by_role('button', name='Close document', exact=True).click()
                    expect(dialog).to_have_count(0)
                    expect(last).to_be_focused()
                    files.click()
                    expect(inventory).to_be_hidden()
                    files.click()
                    expect(inventory.get_by_role('button')).to_have_count(11)
                    page.reload(wait_until='domcontentloaded')
                    final.get_by_role('button', name='Files', exact=True).click(timeout=30000)
                    expect(inventory.get_by_role('button')).to_have_count(11)
                    for name in payloads:
                        expect(inventory.get_by_text(name, exact=True)).to_be_visible()
                    last.click()
                    expect(dialog).to_be_visible()
                    expect(dialog.locator('.document-reader-body')).to_contain_text(payloads['part-11.txt'].decode().strip())
                    assert downloads == [download]
                    page.screenshot(path=str(evidence / 'eleventh-file-reader-after-reload.png'), animations='disabled')
                    assert '#project/' not in page.url
                    receipt['stages'].append('Inventory survives close/reopen and Main reload')
                finally:
                    _m1660_material_finish(page, server, evidence, receipt)
                    browser.close()
        finally:
            server.stop()


def _m1660_seed_readable_inventory(root, repo, answer='Read the retained files from this completed result.'):
    from ouroboros.artifacts import collect_task_artifact_records, store_task_artifact_bytes
    from ouroboros.project_dialogue import project_completion_delivery_outcome
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.core import _write_file
    from ouroboros.tools.registry import ToolContext
    from supervisor.message_bus import log_chat

    task_id = 'readable-inventory-task'
    project = create_project(root, 'readable-inventory', name='Readable inventory')
    bind_task_to_project(root, task_id, project['id'], project['chat_id'], origin={'absent': 'system'})
    payloads = {f'part-{index:02d}.txt': f'Retained result number {index}.\n'.encode() for index in range(1, 12)}
    for name, payload in payloads.items():
        store_task_artifact_bytes(root, task_id, name=name, data=payload)
    nested = {'research/brief.txt': 'The nested brief is individually readable.\n',
              'research/appendix.txt': 'The separate appendix has different bytes.\n'}
    ctx = ToolContext(repo_dir=repo, drive_root=root, task_id=task_id)
    producer_receipts = []
    for path, body in nested.items():
        produced = _write_file(ctx, root='artifact_store', path=path, content=body)
        producer_receipts.append(produced)
        assert produced.startswith('OK: wrote artifact_store:'), produced
        payloads[path] = body.encode()
    records = collect_task_artifact_records(root, task_id, strict=True)
    assert {'research/brief.txt', 'research/appendix.txt'} <= {row.get('relpath') for row in records}
    result = write_task_result(root, task_id, 'completed', project_id=project['id'], chat_id=project['chat_id'],
        terminal_origin='model_final', result=answer,
        artifacts=records, outcome_axes={'execution': {'status': 'ok'}})
    log_chat('out', project['chat_id'], 1, result['result'], task_id=task_id, drive_root=root, require_write=True)
    task = {'id': task_id, 'project_id': project['id'], 'chat_id': project['chat_id'], 'root_task_id': task_id}
    disposition, _queued = project_completion_delivery_outcome(root, {}, task_id, task, result, {'status': 'completed'})
    assert disposition == 'owed'
    return {'task_id': task_id, 'project': project, 'registered_artifacts': records,
            'nested_producer_receipts': producer_receipts, 'producer_disposition': disposition,
            'completion_answer': result['result'], 'stages': [], 'downloads': []}, payloads


@pytest.mark.parametrize('selection', ['root-file', 'nested-member'])
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_main_files_retained_text_opens_inapp_reader_with_project_closed(wait_clone, tmp_path, selection, engine):
    """Activate Main's file UI; require an in-app reader, never a download substitute."""
    from playwright.sync_api import expect, sync_playwright

    root = tmp_path / 'instance' / 'data'
    root.mkdir(parents=True)
    repo = tmp_path / 'artifact-producer-repo'
    repo.mkdir()
    receipt, payloads = _m1660_seed_readable_inventory(root, repo)
    receipt.update(selection=selection, engine=engine)
    evidence = _m1660_material_evidence(tmp_path, f'files-reader-{engine}-{selection}')
    with ScriptedStubModel([{'final': 'Unused model fixture.'}]) as stub:
        settings_path = root / 'settings.json'
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={'width': 1100, 'height': 900}, reduced_motion='reduce')
                downloads = []
                page.on('download', lambda download: downloads.append(download))
                try:
                    page.goto(server.base_url, wait_until='domcontentloaded')
                    final = page.locator('.chat-bubble.project-answer').filter(has_text=receipt['completion_answer'])
                    final.wait_for(timeout=30000)
                    final.get_by_role('button', name='Files', exact=True).click()
                    inventory = final.locator('.project-answer-files')
                    inventory.get_by_text('Available files', exact=True).wait_for()
                    assert not page.locator('#project-panel').is_visible()
                    receipt['inventory_before'] = inventory.inner_text()
                    receipt['inventory_controls_before'] = inventory.locator('a, button').evaluate_all(
                        '(nodes) => nodes.map(node => ({tag: node.tagName, text: node.textContent, href: node.getAttribute("href"), download: node.getAttribute("download")}))')
                    page.screenshot(path=str(evidence / 'inventory-before-activation.png'), animations='disabled')
                    path = 'part-11.txt' if selection == 'root-file' else 'research/brief.txt'
                    suffix = path.rsplit('/', 1)[-1]
                    url = f'/api/tasks/{receipt["task_id"]}/artifacts/{suffix}'
                    if selection == 'nested-member':
                        url += '?relpath=research/brief.txt'
                    # This proves fixture readiness only. It is not counted as
                    # consumer access and does not satisfy the reader assertions.
                    diagnostic = page.request.get(server.base_url + url)
                    receipt['fixture_readiness_only'] = {'url': url, 'status': diagnostic.status,
                        'sha256': hashlib.sha256(diagnostic.body()).hexdigest()}
                    assert diagnostic.status == 200 and diagnostic.body() == payloads[path]
                    if selection == 'nested-member':
                        inventory.get_by_text('research/', exact=True).click()
                        receipt['stages'].append('Activated the retained research folder from Main Files')
                        member = inventory.get_by_text('research/brief.txt', exact=True)
                        expect(member, 'Opening a Main Files folder must expose its individual retained files').to_be_visible(timeout=5000)
                        member.click()
                    else:
                        inventory.get_by_text('part-11.txt', exact=True).click()
                        receipt['stages'].append('Activated the eleventh retained text file from Main Files')
                    dialog = page.locator('dialog.document-reader')
                    expect(dialog, 'Main Files must open the existing in-app reader; downloading is not reading in Main').to_be_visible(timeout=5000)
                    expect(dialog.locator('.document-reader-body')).to_contain_text(payloads[path].decode().strip())
                    assert downloads == [], 'Opening a retained file must read it without downloading'
                    assert not page.locator('#project-panel').is_visible()
                    assert page.url == server.base_url + '/'
                    page.screenshot(path=str(evidence / 'reader.png'), animations='disabled')
                    receipt['stages'].append('Exact selected text visible in existing reader with Project closed')
                    with page.expect_download() as download_info:
                        dialog.get_by_role('button', name='Download', exact=True).click()
                    download = download_info.value
                    assert downloads == [download], 'Only the explicit Download action may download the file'
                    downloaded = evidence / f'downloaded-{suffix}'
                    download.save_as(str(downloaded))
                    assert downloaded.read_bytes() == payloads[path]
                    receipt['download_sha256'] = hashlib.sha256(downloaded.read_bytes()).hexdigest()
                    receipt['stages'].append('Explicit reader Download returned exact selected retained bytes')
                    page.keyboard.press('Escape')
                    expect(dialog).to_have_count(0)
                    entry = inventory.locator(f'[data-result-file="{path}"]').get_by_role('button')
                    expect(entry).to_be_focused()
                    page.reload(wait_until='domcontentloaded')
                    final.get_by_role('button', name='Files', exact=True).click(timeout=30000)
                    if selection == 'nested-member':
                        inventory.get_by_text('research/', exact=True).click()
                    entry.click()
                    expect(dialog).to_be_visible()
                    expect(dialog.locator('.document-reader-body')).to_contain_text(payloads[path].decode().strip())
                    assert downloads == [download]
                    assert not page.locator('#project-panel').is_visible()
                    assert page.url == server.base_url + '/'
                    page.screenshot(path=str(evidence / 'reader-after-reload.png'), animations='disabled')
                    receipt['stages'].append('Selected file remains readable after Main reload with Project closed')
                finally:
                    for index, download in enumerate(downloads):
                        name = Path(download.suggested_filename).name
                        destination = evidence / f'observed-download-{index}-{name}'
                        try:
                            download.save_as(str(destination))
                            receipt['downloads'].append({'name': name, 'saved': destination.name,
                                'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
                        except Exception as exc:
                            receipt['downloads'].append({'name': name, 'error': str(exc)})
                    _m1660_material_finish(page, server, evidence, receipt)
                    browser.close()
        finally:
            server.stop()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_main_final_released_with_its_history_page_mounts_again_with_files_and_fold(wait_clone, tmp_path, engine):
    """Four older pages release the page holding Main's final; reading it again mounts the
    answer, its fold and its Files once more, and a transport retry stays one outcome."""
    import re
    import time
    from datetime import datetime, timedelta, timezone
    from playwright.sync_api import expect, sync_playwright
    from tests.test_chat_history_paging_browser import MAIN, _idle, _open, _step, _write
    from tests.ui_chat_viewport_smoke import _emit_ws_frame

    root = tmp_path / 'instance' / 'data'
    root.mkdir(parents=True)
    repo = tmp_path / 'artifact-producer-repo'
    repo.mkdir()
    # Four Main pages older than the final: one history read holds 150 conversation rows.
    _write(root / 'archive' / 'chat_20260901T000000.jsonl', [
        {'ts': '2026-09-01T10:00:00Z', 'chat_id': 1, 'direction': 'in', 'text': f'Older Main message {index:04d}'}
        for index in range(620)])
    answer = '\n\n'.join(f'Paragraph {index}: the retained reports are ready to read.' for index in range(1, 31))
    receipt, payloads = _m1660_seed_readable_inventory(root, repo, answer=answer)
    receipt.update(engine=engine)
    evidence = _m1660_material_evidence(tmp_path, f'final-page-release-{engine}')
    with ScriptedStubModel([{'final': 'Unused model fixture.'}]) as stub:
        settings_path = root / 'settings.json'
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            # The server publishes the owed final itself; its row carries the delivery identity.
            deadline, final_row = time.monotonic() + 90, None
            while final_row is None and time.monotonic() < deadline:
                rows = _api(server.base_url, 'GET', '/api/chat/history?chat_id=1').get('messages') or []
                final_row = next((row for row in rows if row.get('system_type') == 'project_completion_summary'
                                  and row.get('task_id') == receipt['task_id']), None)
                time.sleep(0 if final_row else 0.5)
            assert final_row and final_row.get('delivery_id'), 'the owed final was not published'
            receipt['final_row'] = final_row
            server.stop()
            # Newer Main conversation puts the final one page back from the newest read.
            start = datetime.now(timezone.utc)
            with (root / 'logs' / 'chat.jsonl').open('a', encoding='utf-8') as stream:
                for index in range(160):
                    stream.write(json.dumps({'ts': (start + timedelta(seconds=index)).isoformat(), 'chat_id': 1,
                                             'direction': 'in', 'text': f'Newer Main message {index:04d}'}) + '\n')
            server.start(ready_timeout=120)
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={'width': 1100, 'height': 900}, reduced_motion='reduce')
                try:
                    _open(page, server.base_url)
                    final = page.locator(f'{MAIN} .chat-bubble.project-answer[data-delivery-id="{final_row["delivery_id"]}"]')

                    def readable(stage):
                        expect(final).to_have_count(1)
                        expect(final.locator('.message')).to_contain_text('Paragraph 1: the retained reports')
                        final.get_by_role('button', name='Show full', exact=True).click()
                        expect(final).to_have_class(re.compile(r'\bis-expanded\b'))
                        assert final.locator('.message').evaluate('node => node.scrollHeight <= node.clientHeight + 1')
                        final.get_by_role('button', name='Files', exact=True).click()
                        final.locator('.project-answer-files').get_by_text('part-11.txt', exact=True).click()
                        dialog = page.locator('dialog.document-reader')
                        expect(dialog.locator('.document-reader-body')).to_contain_text(
                            payloads['part-11.txt'].decode().strip())
                        page.screenshot(path=str(evidence / f'{stage}.png'), animations='disabled')
                        page.keyboard.press('Escape')
                        expect(dialog).to_have_count(0)
                        # Folded and unfocused again: a focused or visible row protects its page.
                        final.get_by_role('button', name='Show less', exact=True).click()
                        final.get_by_role('button', name='Files', exact=True).click()
                        page.evaluate('() => document.activeElement?.blur()')
                        receipt['stages'].append(f'{stage}: answer, fold, Files and reader readable')

                    expect(final).to_have_count(0)
                    _step(page, MAIN, automatic=True)
                    readable('first-read')
                    for _ in range(3):
                        _step(page, MAIN, automatic=True)
                    expect(final).to_have_count(0)
                    receipt['stages'].append('four older pages released the page holding the final')
                    page.locator('#chat-scroll-bottom').click()
                    _idle(page, MAIN)
                    _step(page, MAIN, automatic=True)
                    readable('read-again')
                    # A transport retry of the mounted delivery is the same outcome at its original time.
                    stamp = final.get_attribute('data-ts')
                    _emit_ws_frame(page, {
                        'type': 'chat', 'chat_id': 1, 'role': 'system', 'content': final_row.get('text') or '',
                        'ts': datetime.now(timezone.utc).isoformat(), 'markdown': bool(final_row.get('markdown')),
                        **{key: final_row[key] for key in ('system_type', 'task_id', 'project_id', 'project_name',
                                                           'task_name', 'completion_answer', 'completion_reason',
                                                           'terminal_time', 'delivery_id') if key in final_row}})
                    expect(final).to_have_count(1)
                    assert final.get_attribute('data-ts') == stamp
                    receipt['stages'].append('a transport retry kept one outcome at its original time')
                    page.reload(wait_until='domcontentloaded')
                    _idle(page, MAIN)
                    _step(page, MAIN, automatic=True)
                    readable('after-reload')
                finally:
                    _m1660_material_finish(page, server, evidence, receipt)
                    browser.close()
        finally:
            server.stop()


# The Files read waits until the test releases it, so the inventory lands while the reader is elsewhere.
_M1660_HOLD_DETAIL = """task => {
    const fetch = window.fetch.bind(window);
    window.__holdDetail = false;
    window.fetch = async (input, init) => {
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        if (window.__holdDetail && url.pathname === `/api/tasks/${task}`) {
            window.__holdDetail = false;
            await new Promise(resolve => { window.__releaseDetail = resolve; });
        }
        return fetch(input, init);
    };
}"""
_M1660_TOP = """node => node.getBoundingClientRect().top
    - document.querySelector('#chat-messages').getBoundingClientRect().top"""
_M1660_PLACE = """(node, offset) => { const feed = document.querySelector('#chat-messages');
    feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - offset; }"""
_M1660_REMAINING = """() => { const feed = document.querySelector('#chat-messages');
    return feed.scrollHeight - feed.clientHeight - feed.scrollTop; }"""


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_main_files_landing_above_the_reader_keeps_the_message_being_read(wait_clone, tmp_path, engine):
    """Files opened on Main's final, then reading on below: the inventory that lands later above the
    reader keeps the selected message in place with native scroll anchoring off (as in WKWebView),
    a folder opens in place, and a reader back at the live edge keeps following."""
    import time
    from datetime import datetime, timedelta, timezone
    from playwright.sync_api import expect, sync_playwright
    from tests.test_chat_history_paging_browser import MAIN, _idle, _open
    from tests.ui_chat_viewport_smoke import _SETTLE_RESTORE_FRAMES

    root = tmp_path / 'instance' / 'data'
    root.mkdir(parents=True)
    repo = tmp_path / 'artifact-producer-repo'
    repo.mkdir()
    receipt, _payloads = _m1660_seed_readable_inventory(root, repo)
    receipt.update(engine=engine, measures={})
    evidence = _m1660_material_evidence(tmp_path, f'files-anchor-{engine}')
    with ScriptedStubModel([{'final': 'Unused model fixture.'}]) as stub:
        settings_path = root / 'settings.json'
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            deadline, final_row = time.monotonic() + 90, None
            while final_row is None and time.monotonic() < deadline:
                rows = _api(server.base_url, 'GET', '/api/chat/history?chat_id=1').get('messages') or []
                final_row = next((row for row in rows if row.get('system_type') == 'project_completion_summary'
                                  and row.get('task_id') == receipt['task_id']), None)
                time.sleep(0 if final_row else 0.5)
            assert final_row and final_row.get('delivery_id'), 'the owed final was not published'
            server.stop()
            start = datetime.now(timezone.utc)
            with (root / 'logs' / 'chat.jsonl').open('a', encoding='utf-8') as stream:
                for index in range(40):
                    stream.write(json.dumps({'ts': (start + timedelta(seconds=index)).isoformat(), 'chat_id': 1,
                                             'direction': 'in', 'text': f'Later Main message {index:02d}'}) + '\n')
            server.start(ready_timeout=120)
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={'width': 1100, 'height': 900}, reduced_motion='reduce')
                page.add_init_script(f"({_M1660_HOLD_DETAIL})({json.dumps(receipt['task_id'])})")
                feed = page.locator(MAIN)
                final = page.locator(f'{MAIN} .chat-bubble.project-answer'
                                     f'[data-delivery-id="{final_row["delivery_id"]}"]')
                files = final.get_by_role('button', name='Files', exact=True)
                inventory = final.locator('.project-answer-files')

                def settle():
                    page.evaluate(_SETTLE_RESTORE_FRAMES)

                def ready():
                    page.wait_for_selector(f'{MAIN}[data-history-hydrated="true"]')
                    page.add_style_tag(content='#chat-messages, #chat-messages * { overflow-anchor: none !important; }')
                    receipt['measures']['overflow_anchor'] = feed.evaluate(
                        "node => [getComputedStyle(node).overflowAnchor, CSS.supports('overflow-anchor: none')]")
                    assert receipt['measures']['overflow_anchor'] in (['none', True], [None, False])
                    settle()
                    feed.hover()
                    page.mouse.wheel(0, -120)  # a real gesture leaves the live edge and owns the position
                    settle()
                    final.evaluate(_M1660_PLACE, 120)  # Files on screen, far from the live edge
                    settle()

                def open_held():
                    before = files.evaluate(_M1660_TOP)
                    page.evaluate('() => { window.__holdDetail = true; }')
                    files.click()
                    expect(inventory).to_have_text('Loading available files…')
                    assert abs(files.evaluate(_M1660_TOP) - before) <= 1, 'the expansion moved its own control'
                    settle()
                    expect(inventory).to_have_text('Loading available files…')  # this read is the held one

                def landed():
                    page.evaluate('() => window.__releaseDetail()')
                    inventory.get_by_text('Available files', exact=True).wait_for(state='attached')
                    settle()
                    return inventory.evaluate('node => node.getBoundingClientRect().height')

                try:
                    _open(page, server.base_url)
                    ready()
                    open_held()
                    # Read on below the final and select the message being read.
                    later = page.locator(f'{MAIN} .chat-bubble').filter(has_text='Later Main message 06')
                    later.evaluate(_M1660_PLACE, 40)
                    settle()
                    assert final.evaluate('node => node.getBoundingClientRect().bottom') < feed.evaluate(
                        'node => node.getBoundingClientRect().top'), 'the final must be above the reader'
                    later.locator('.message').click(click_count=3)
                    selected = page.evaluate('() => window.getSelection().toString().trim()')
                    assert selected == 'Later Main message 06'
                    focused = '() => document.activeElement?.outerHTML.slice(0, 80)'
                    before, focus = later.evaluate(_M1660_TOP), page.evaluate(focused)
                    page.screenshot(path=str(evidence / 'reading-below-while-files-load.png'), animations='disabled')
                    height = landed()
                    after = later.evaluate(_M1660_TOP)
                    receipt['measures']['selected'] = {'before': before, 'after': after, 'inventory_height': height}
                    page.screenshot(path=str(evidence / 'files-landed-above.png'), animations='disabled')
                    assert height > 300 and abs(after - before) <= 1, receipt['measures']['selected']
                    assert page.evaluate('() => window.getSelection().toString().trim()') == selected
                    assert page.evaluate(focused) == focus
                    receipt['stages'].append('a held inventory landed above the selected message; it stayed put')
                    # A folder opens below its own summary: nothing the reader is on moves.
                    folder = inventory.locator('summary').filter(has_text='research/')
                    folder.evaluate(_M1660_PLACE, 300)
                    settle()
                    tops = [folder.evaluate(_M1660_TOP), files.evaluate(_M1660_TOP)]
                    folder.click()
                    expect(inventory.get_by_text('research/brief.txt', exact=True)).to_be_visible()
                    settle()
                    assert [folder.evaluate(_M1660_TOP), files.evaluate(_M1660_TOP)] == pytest.approx(tops, abs=1)
                    # Closing and reopening Files keeps its control under the pointer.
                    files.evaluate(_M1660_PLACE, 200)
                    settle()
                    top = files.evaluate(_M1660_TOP)
                    files.click()
                    expect(inventory).to_be_hidden()
                    assert abs(files.evaluate(_M1660_TOP) - top) <= 1, 'closing Files moved its control'
                    files.click()
                    expect(inventory.get_by_text('research/brief.txt', exact=True)).to_be_visible()
                    assert abs(files.evaluate(_M1660_TOP) - top) <= 1, 'reopening Files moved its control'
                    page.screenshot(path=str(evidence / 'folder-open-files-reopened.png'), animations='disabled')
                    receipt['stages'].append('a folder opened in place; Files closed and reopened under the pointer')
                    # Back at the live edge, an inventory landing above keeps following the newest message.
                    page.reload(wait_until='domcontentloaded')
                    page.wait_for_function(
                        '() => window.__testSockets?.some(socket => socket.readyState === WebSocket.OPEN)')
                    _idle(page, MAIN)
                    ready()
                    open_held()
                    page.locator('#chat-scroll-bottom').click()
                    page.wait_for_function(f'() => ({_M1660_REMAINING})() <= 6')
                    settle()
                    height = landed()
                    remaining = page.evaluate(_M1660_REMAINING)
                    receipt['measures']['live_edge'] = {'remaining': remaining, 'inventory_height': height}
                    page.screenshot(path=str(evidence / 'live-edge-after-landing.png'), animations='disabled')
                    assert height > 300 and remaining <= 6, receipt['measures']['live_edge']
                    newest = page.locator(f'{MAIN} .chat-bubble').filter(has_text='Later Main message 39')
                    expect(newest).to_be_in_viewport()
                    receipt['stages'].append('at the live edge the landing inventory kept the newest message in view')
                finally:
                    _m1660_material_finish(page, server, evidence, receipt)
                    browser.close()
        finally:
            server.stop()
