import json
from pathlib import Path

import numpy as np

from cortex.cards import card_input, load_cards, write_cards
from cortex.llm import fake_llm, parse_json_object
from cortex.profiles import bhavaprakasha as bp
from cortex.search import BM25, Searcher, build_index, rrf, tokenize
from cortex.tree import Tree, build_chunks, chunk_text, parse_markdown

MD = """intro line

## Names
हरीतकी, अभया, पथ्या

## Properties
### Rasa
पाँच रस, लवण रहित
### Virya
उष्ण वीर्य

## Uses
Used for constipation.
"""


def small_tree():
    t = Tree("Book")
    from cortex.tree import Node
    g = t.add(Node(id="g1", title="Group", kind="group"), t.root)
    h = t.add(Node(id="h1", title="Haritaki", kind="section"), g)
    parse_markdown(MD, t, h, "h1")
    build_chunks(t)
    return t


def test_parse_markdown_nesting_and_own_text():
    t = small_tree()
    h = t.nodes["h1"]
    assert h.text == "intro line"
    titles = [t.nodes[c].title for c in h.children]
    assert titles == ["Names", "Properties", "Uses"]
    props = t.nodes[h.children[1]]
    assert [t.nodes[c].title for c in props.children] == ["Rasa", "Virya"]
    assert t.path_str(props.children[1]) == "Book › Group › Haritaki › Properties › Virya"


def test_code_fence_comments_are_not_headings():
    t = Tree("Doc")
    parse_markdown("# Setup\n```bash\n# install deps\npip install x\n```\n## Use\nrun it\n", t, t.root, "n")
    titles = [n.title for n in t.nodes.values()]
    assert titles == ["Doc", "Setup", "Use"]
    assert "# install deps" in t.nodes["n.1"].text


def test_ingest_text_without_headings_makes_parts(tmp_path):
    from cortex.ingest import build_tree
    f = tmp_path / "notes.txt"
    f.write_text("\n\n".join(f"paragraph {i} " + "word " * 120 for i in range(12)))
    t = build_tree(f)
    parts = [t.nodes[c] for c in t.root.children]
    assert len(parts) >= 2 and all(p.meta.get("synthetic_title") for p in parts)


def test_ingest_numbered_headings(tmp_path):
    from cortex.ingest import build_tree
    f = tmp_path / "spec.txt"
    f.write_text("1. Introduction\nsome text\n2. Method\nmore text\n2.1 Data\ndata text\n")
    t = build_tree(f)
    assert [t.nodes[c].title for c in t.root.children][:2] == ["1. Introduction", "2. Method"]


def test_split_claims_keeps_citations_and_skips_code():
    from cortex.verify import split_claims
    ans = ("RAPTOR uses Gaussian mixtures [1]. It picks clusters with BIC [1][2].\n"
           "```bash\npython run.py [3]\n```\n- Collapsed tree beats traversal [3]\nNo citation here at all.")
    cl = split_claims(ans)
    assert [c.cites for c in cl] == [[1], [1, 2], [3], []]
    assert all("python run.py" not in c.text for c in cl)


def test_split_claims_abbreviations_numbers_headings_and_scripts():
    from cortex.verify import split_claims
    ans = ("**Main contributions**\n"
           "- Large gains (e.g., +20 % on QuALITY) and 82.6 % accuracy [2].\n"
           "Smith et al. report it [1]. Second claim follows here [3].\n"
           "हरीतकी त्रिदोषशामक है [1]। यह रसायन भी है [2]।\n"
           "これはテストの文です[1]。二番目の文章です[2]。")
    cl = split_claims(ans)
    assert [c.cites for c in cl] == [[2], [1], [3], [1], [2], [1], [2]]
    assert cl[0].text.startswith("- Large gains (e.g., +20")


def test_verify_recites_and_flags():
    from cortex.verify import verify
    ans = "Uses GMM clustering [2]. Number of clusters via BIC [1]. Works with Neo4j [1]."
    sources = [{"path": "A", "text": "GMM and BIC"}, {"path": "B", "text": "other"}]

    def judge(system, user):
        return json.dumps({"verdicts": [
            {"claim": 1, "verdict": "unsupported", "supported_by": [1]},
            {"claim": 2, "verdict": "supported", "supported_by": [1]},
            {"claim": 3, "verdict": "unsupported", "supported_by": [], "note": "no Neo4j"},
        ]})

    out, rep = verify(judge, ans, sources)
    assert out.startswith("Uses GMM clustering [1]. Number of clusters via BIC [1].")
    assert "Works with Neo4j [1]. ⚠ not found in the sources" in out
    assert (rep["supported"], rep["recited"], rep["unsupported"]) == (1, 1, 1)


def test_verify_never_blocks_on_bad_judge():
    from cortex.verify import verify
    ans = "The method clusters chunks with GMM [1]."
    out, rep = verify(lambda s, u: "not json", ans, [{"path": "A", "text": "x"}])
    assert out == ans and "error" in rep


def test_bottom_up_puts_children_first():
    t = small_tree()
    seen = set()
    for level in t.bottom_up():
        for nid in level:
            assert all(c in seen for c in t.nodes[nid].children)
        seen.update(level)


def test_chunk_text_respects_limit_and_keeps_text():
    para = "श्लोक एक । " * 200
    chunks = chunk_text(f"short para\n\n{para}\n\nlast", max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    joined = " ".join(chunks)
    assert "short para" in joined and "last" in joined


def test_save_load_roundtrip(tmp_path):
    t = small_tree()
    t.save(tmp_path / "tree.json")
    t2 = Tree.load(tmp_path / "tree.json")
    assert t2.path_str("h1.3") == t.path_str("h1.3")
    assert len(t2.chunks) == len(t.chunks)


def test_varga_detection_and_assignment():
    full = "\n".join([
        "अथ हरीतक्यादिवर्गः",       # 1
        "हरीतकी text",              # 2
        "हरीतक्यादिवर्गः",           # 3 running header
        "more text",                # 4
        "गुडूच्यादिवर्ग:",            # 5 OCR'd visarga
        "text",                     # 6
        "षोडशस्त वर्गः समाप्तः ॥ १६ ॥",  # 7 end marker, ignored
    ])
    headers = bp.varga_headers(full)
    assert headers == [(1, "हरीतक्यादि"), (3, "हरीतक्यादि"), (5, "गुडूच्यादि")]
    assert bp.assign_varga(2, 4, headers) == "हरीतक्यादि"
    assert bp.assign_varga(6, 6, headers) == "गुडूच्यादि"


def test_smooth_fixes_isolated_outlier():
    assert bp.smooth(["a", "a", "b", "a", None, "c"]) == ["a", "a", "a", "a", "a", "c"]


def test_bhavaprakasha_build(tmp_path):
    herbs = tmp_path / "herbs"
    herbs.mkdir()
    (herbs / "001_हरीतकी.txt").write_text("# Lines: 1-2\n# Corrected by X\n# हरीतकी\n\n## नामावली\nअभया\n", encoding="utf-8")
    (herbs / "002_गुडूची.txt").write_text("# Lines: 5-6\n# गुडूची\n\n## गुण\nतिक्त\n", encoding="utf-8")
    (herbs / "453a_अयापान.txt").write_text("# Herb: अयापान\n\n## Uses\nx\n", encoding="utf-8")
    full = tmp_path / "full.txt"
    full.write_text("अथ हरीतक्यादिवर्गः\nx\nx\nx\nगुडूच्यादिवर्गः\nx\n", encoding="utf-8")
    t = bp.build(herbs, full)
    groups = [t.nodes[g].title for g in t.root.children]
    assert groups == ["हरीतक्यादिवर्ग", "गुडूच्यादिवर्ग", bp.APPENDIX]
    assert t.path_str("h001.1") .endswith("हरीतक्यादिवर्ग › हरीतकी › नामावली")
    assert t.nodes["h001"].meta["lines"] == [1, 2]


def test_parse_json_object_handles_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}


def test_cards_bottom_up_and_cached(tmp_path):
    t = small_tree()
    calls = []

    def llm(system, user):
        calls.append(user)
        return fake_llm(system, user)

    out = tmp_path / "cards.jsonl"
    cards = write_cards(t, llm, out, workers=2, log=lambda *_: None)
    assert set(cards) == set(t.nodes)
    # Parent input includes children's summaries.
    assert "SUBSECTIONS" in card_input(t, "h1", cards)
    n = len(calls)
    write_cards(t, llm, out, workers=2, log=lambda *_: None)
    assert len(calls) == n  # nothing rewritten on resume
    assert len(load_cards(out)) == len(t.nodes)


def test_short_leaves_skip_the_model(tmp_path):
    t = small_tree()
    calls = []

    def llm(system, user):
        calls.append(user)
        return fake_llm(system, user)

    cards = write_cards(t, llm, tmp_path / "cards.jsonl", log=lambda *_: None)
    leaves = [n for n in t.nodes.values() if not n.children]
    assert all(cards[n.id].get("auto") for n in leaves)  # all leaves here are short
    assert cards["h1.4"]["summary"] == "उष्ण वीर्य"  # Properties › Virya
    assert len(calls) == len(t.nodes) - len(leaves)


def test_daily_limit_stops_cleanly_and_resumes(tmp_path):
    from cortex.cards import write_cards_by_section
    from cortex.llm import DailyLimit
    import pytest
    t = small_tree()
    budget = {"n": 2}

    def llm(system, user):
        if budget["n"] == 0:
            raise DailyLimit("Rate limit reached ... requests per day (RPD)")
        budget["n"] -= 1
        return fake_llm(system, user)

    out = tmp_path / "cards.jsonl"
    with pytest.raises(DailyLimit):
        write_cards_by_section(t, llm, out, log=lambda *_: None, workers=1)
    assert 0 < len(load_cards(out)) < len(t.nodes)
    budget["n"] = 100  # next day
    write_cards_by_section(t, llm, out, log=lambda *_: None, workers=1)
    assert set(load_cards(out)) == set(t.nodes)


def test_tokenize_adds_prefix_stems():
    toks = tokenize("हरीतक्या हरीतकी।")
    assert "हरीतकी" in toks and "हरीतक्या" in toks
    assert toks.count("हरीतक") == 2  # shared prefix links inflections


def test_cjk_tokens_are_bigrams():
    toks = tokenize("光合成は植物が行う。")
    assert "光合" in toks and "合成" in toks and "植物" in toks
    assert BM25([tokenize("光合成は植物が行う"), tokenize("動物の呼吸")]).scores(tokenize("光合成"))[0] > 0


def test_wiki_headings(tmp_path):
    from cortex.ingest import build_tree
    f = tmp_path / "w.txt"
    f.write_text("Intro text.\n\n== History ==\nold\n\n=== Early ===\nearly\n\n== Uses ==\nuse\n")
    t = build_tree(f)
    assert [t.nodes[c].title for c in t.root.children] == ["History", "Uses"]
    assert t.nodes[t.root.children[0]].children


def test_numbered_headings_nest_by_depth(tmp_path):
    from cortex.ingest import build_tree
    f = tmp_path / "rfc.txt"
    f.write_text("1. Scope\na\n2. Protocol\nb\n2.1 Methods\nc\n2.1.1 BREW method\nd\n3. Security\ne\n")
    t = build_tree(f)
    tops = [t.nodes[c].title for c in t.root.children]
    assert tops == ["1. Scope", "2. Protocol", "3. Security"]
    proto = t.nodes[t.root.children[1]]
    sub = t.nodes[proto.children[0]]
    assert sub.title == "2.1 Methods" and t.nodes[sub.children[0]].title == "2.1.1 BREW method"


def test_metadata_comment_lines_are_not_headings(tmp_path):
    from cortex.ingest import build_tree
    f = tmp_path / "herb.md"
    f.write_text("# Lines: 25-256\n# Corrected by: Qwen\n# हरीतकी\n\n## नामावली\nअभया\n")
    t = build_tree(f)
    assert [t.nodes[c].title for c in t.root.children] == ["हरीतकी"]
    assert t.root.meta["lines"] == "25-256"


def test_bm25_and_rrf():
    docs = [tokenize(s) for s in ["apple banana", "banana cherry cherry", "dates"]]
    s = BM25(docs).scores(tokenize("cherry"))
    assert int(np.argmax(s)) == 1 and s[2] == 0
    fused = rrf([[0, 1], [1, 2]])
    assert max(fused, key=fused.get) == 1


def fake_embedder(texts, role):
    vecs = []
    for t in texts:
        v = np.zeros(64, np.float32)
        for tok in tokenize(t):
            v[hash(tok) % 64] += 1
        vecs.append(v / (np.linalg.norm(v) or 1))
    return np.array(vecs, np.float32)


def test_index_and_search_end_to_end(tmp_path):
    t = small_tree()
    cards = write_cards(t, fake_llm, tmp_path / "cards.jsonl", log=lambda *_: None)
    build_index(t, cards, tmp_path / "index", fake_embedder)
    s = Searcher(tmp_path / "index", fake_embedder)
    hits = s.search("उष्ण वीर्य", k=3)
    assert hits and "Virya" in hits[0]["path"]
    assert all(h["kind"] == "card" for h in s.search("constipation", kind="card"))


def test_parse_json_tolerates_raw_newlines_in_strings():
    assert parse_json_object('{"summary": "line one\nline two"}')["summary"] == "line one\nline two"


def test_list_items_inherit_block_citation():
    from cortex.verify import split_claims
    ans = ("The eight branches are:\n- Kayachikitsa internal medicine\n- Shalya surgery branch\n[1]\n\n"
           "- Powder dose is 2.5 to 7.5 g\n- Decoction dose is 150 ml\nThese doses come from the table [2][3].\n\n"
           "A separate uncited paragraph here.")
    cl = split_claims(ans)
    assert [c.cites for c in cl] == [[1], [1], [2, 3], [2, 3], [2, 3], []]
    assert [c.inherited for c in cl][:2] == [True, True]


def test_list_inherits_from_closing_block_after_blank_line():
    from cortex.verify import split_claims
    ans = ("गिलोय सत्व बनाने की विधि:\n\n1. बरसात से पहले गुडूची एकत्रित करें।\n"
           "2. छाल हटाकर पानी में धोएँ।\n\nयह प्रक्रिया स्रोत [5] में वर्णित है।\n\n"
           "- कायचिकित्सा आंतरिक चिकित्सा\n- शल्यतन्त्र शल्य चिकित्सा\n\n[1]")
    cl = split_claims(ans)
    assert [c.cites for c in cl] == [[5], [5], [5], [1], [1]]


def test_running_headers_are_dropped():
    from cortex.ingest import _drop_page_furniture
    pages = [f"M T W T F S S\n## Topic {i}\nbody text {i}\nहरीतक्यादिवर्गः {i + 10}" for i in range(6)]
    out = _drop_page_furniture(pages)
    assert all("M T W" not in p and "वर्गः" not in p for p in out)
    assert all(f"## Topic {i}" in p for i, p in enumerate(out))


def test_router_detects_overview_questions():
    from cortex.ask import route
    for q in ["Summarize this paper", "What is this document about?", "मुख्य बिंदु क्या हैं", "この文書の要約", "Give me the key takeaways"]:
        assert route(q) == "overview", q
    for q in ["What is the dose of Guduchi?", "Which error code does a teapot return?"]:
        assert route(q) == "search", q


def test_entity_index_and_hits():
    from cortex.ask import Doc, build_entity_index, entity_hits
    t = small_tree()
    cards = {"h1.4": {"entities": ["Virya (concept)"], "key_terms": []},
             "h1.5": {"entities": ["Constipation (condition)"], "key_terms": ["Uses"]}}
    idx = build_entity_index(cards)
    assert idx["virya"] == ["h1.4"]
    d = Doc("d", "Doc", t, searcher=None, cards=cards, entities=idx)
    assert entity_hits(d, "Is it good for constipation?") == ["h1.5"]


def test_library_end_to_end_with_fake_model(tmp_path, monkeypatch):
    import pytest
    from pathlib import Path as P
    if not (P.home() / ".cache/huggingface/hub/models--intfloat--multilingual-e5-small").exists():
        pytest.skip("embedding model not cached")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from cortex.library import Library
    doc = tmp_path / "notes.md"
    doc.write_text("# Herbs\n## Ginger\nGinger helps digestion and nausea.\n## Honey\nHoney is cooling and sweet.\n")
    lib = Library(tmp_path / "home")
    slug = lib.add(doc, provider="fake")
    assert lib.meta(slug)["status"] == "ready" and lib.meta(slug)["cards"] == 4
    res = lib.ask("What helps nausea?", provider="fake", verify=False)
    assert any("Ginger" in s["path"] for s in res["sources"][:2])
    lib.feedback(res["id"], 1)
    assert lib.history_items()[0]["feedback"] == 1
    lib.delete(slug)
    assert lib.list() == []


def test_ocr_over_marked_headings_are_demoted():
    from cortex.ingest import _from_paged_text
    from cortex.tree import Tree
    pages = ["## fold1 & fold2 are disjoint\n## k fold gives k pairs\n## C1 C2 C3\n## m1 m2 m3\nshort",
             "## Imputation by mean\n## Feature extraction\n## Experiment design"]
    t = _from_paged_text(Tree("notes"), pages)
    assert "treated as ordinary lines" in t.root.meta["structure"]
    body = " ".join(n.text for n in t.nodes.values())
    assert "k fold gives k pairs" in body and "Imputation by mean" in body
