from extractor.web_scraper import _filter_conversations


def test_panel_selection_matches_whole_names():
    conversations = [{'nickname': n, 'name': n + ' 09:49'} for n in ['n', 'lin', 'Eternal', 'Lean.', '林——meditation']]
    selected = _filter_conversations(conversations, 'n, lin, Eternal')
    assert [c['nickname'] for c in selected] == ['n', 'lin', 'Eternal']
    assert _filter_conversations(conversations, ' , ') == []
    assert _filter_conversations(conversations, 'n 09:49') == conversations[:1]
    assert _filter_conversations(conversations, 'eternal') == []
