"""Split mode `syllabus`: LOs go to the teacher's lessons by pages, otherwise by MiniLM similarity."""
from lo_pipeline.split.syllabus import lesson_ranges, split_by_syllabus

VOCAB = ["pointer", "malloc", "variance", "entropy"]


def fake_embed(texts):
    """One dimension per VOCAB word: how often it appears in the text."""
    return [[t.lower().replace(".", " ").split().count(w) for w in VOCAB] for t in texts]


def no_embed(texts):
    raise AssertionError("MiniLM should not be needed")


def lo(id_, file="a.pdf", page=1, question="", prereqs=()):
    return {"id": id_, "source_title": file, "source_page": page, "question": question, "answer": "",
            "prerequisite_ids": list(prereqs)}


def lesson(title, pages=None, description=None):
    return {"title": title, "pages": pages, "description": description}


def test_lesson_ranges_map_document_numbers_to_files():
    syllabus = [lesson("A", "D1:1-3, D2:5"), lesson("B", "D3:1"), lesson("C")]
    assert lesson_ranges(syllabus, ["a.pdf", "b.pdf"]) == [[("a.pdf", 1, 3), ("b.pdf", 5, 5)], [], []]


def test_los_go_to_lessons_by_pages_in_two_files():
    los = [lo("1", "a.pdf", 2), lo("2", "b.pdf", 1), lo("3", "a.pdf", 7), lo("4", "b.pdf", 9)]
    syllabus = [lesson("Úvod", "D1:1-5"), lesson("Ďalej", "D1:6-10, D2:1-10")]
    s = split_by_syllabus(los, syllabus, ["a.pdf", "b.pdf"], embed=no_embed)
    assert s.lessons == [(0, [0]), (1, [2, 1, 3])]
    assert (s.by_pages, s.by_similarity, s.empty_lessons) == (4, 0, [])


def test_lo_outside_all_ranges_goes_to_most_similar_lesson():
    los = [lo("1", page=1, question="pointer"), lo("2", page=50, question="what does malloc return")]
    syllabus = [lesson("Pointers", "D1:1-10", "pointer basics"), lesson("Memory", "D1:11-20", "malloc and free")]
    s = split_by_syllabus(los, syllabus, ["a.pdf"], embed=fake_embed)
    assert s.lessons == [(0, [0]), (1, [1])]
    assert (s.by_pages, s.by_similarity) == (1, 1)


def test_lessons_without_pages_use_similarity():
    los = [lo("1", question="entropy of a coin"), lo("2", question="variance of a sample")]
    syllabus = [lesson("Variance"), lesson("Entropy")]
    s = split_by_syllabus(los, syllabus, ["a.pdf"], embed=fake_embed)
    assert s.lessons == [(0, [1]), (1, [0])]
    assert s.by_similarity == 2


def test_overlapping_ranges_choose_the_more_similar_lesson():
    los = [lo("1", page=5, question="malloc")]
    syllabus = [lesson("Pointers", "D1:1-6", "pointer"), lesson("Memory", "D1:4-9", "malloc")]
    s = split_by_syllabus(los, syllabus, ["a.pdf"], embed=fake_embed)
    assert s.lessons == [(1, [0])]
    assert s.empty_lessons == [0]
    assert s.by_pages == 1


def test_inside_a_lesson_prerequisites_come_first():
    los = [lo("a", page=1, prereqs=["b"]), lo("b", page=3)]
    s = split_by_syllabus(los, [lesson("All", "D1:1-5")], ["a.pdf"], embed=no_embed)
    assert s.lessons == [(0, [1, 0])]
    assert s.prerequisites_later == 0


def test_prerequisite_in_a_later_lesson_is_only_counted():
    # the teacher puts page 1 first, but its LO needs the LO from page 8
    los = [lo("a", page=1, prereqs=["b"]), lo("b", page=8)]
    s = split_by_syllabus(los, [lesson("One", "D1:1-5"), lesson("Two", "D1:6-10")], ["a.pdf"], embed=no_embed)
    assert s.lessons == [(0, [0]), (1, [1])]
    assert s.prerequisites_later == 1


def test_document_beyond_files_and_empty_inputs():
    los = [lo("1", page=1, question="pointer")]
    # D2 does not exist (the API refuses it; here it is just ignored), so similarity decides
    s = split_by_syllabus(los, [lesson("X", "D2:1-5"), lesson("pointer basics")], ["a.pdf"], embed=fake_embed)
    assert s.lessons == [(1, [0])] and s.empty_lessons == [0]
    assert split_by_syllabus([], [lesson("X")], ["a.pdf"]).empty_lessons == [0]
    assert split_by_syllabus(los, [], ["a.pdf"]).lessons == []
