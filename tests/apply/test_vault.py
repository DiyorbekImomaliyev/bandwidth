"""The vault: what forms ask for, kept on this Mac (SPEC VLT-1..VLT-4)."""

from __future__ import annotations

import os

import pytest

from bandwidth.apply.vault import KEYS, Vault, VaultError, check, same_question

PROFILE = """\
first_name: Ada
last_name: Example
email: ada@example.org
phone: "+44 7700 900123"
phone_country: United Kingdom
country: United Kingdom
city: London
links:
  linkedin: https://www.linkedin.com/in/ada-example
  github: https://github.com/ada-example
education:
  school: Example University
  degree: Bachelor of Science
  major: Computer Science
  graduation: 2027-06
"""
PDF = b"%PDF-1.7\n%fake but has the magic\n"


@pytest.fixture
def vault_dir(tmp_path):
    d = tmp_path / "vault"
    d.mkdir(mode=0o700)
    (d / "profile.yaml").write_text(PROFILE)
    (d / "cv.pdf").write_bytes(PDF)
    return d


def test_loads_profile_and_cv(vault_dir):
    """VLT-1"""
    v = Vault.load(vault_dir)
    assert v.profile.first_name == "Ada" and v.profile.education.school == "Example University"
    assert v.cv == vault_dir / "cv.pdf"
    assert v.value("links.github") == "https://github.com/ada-example"
    assert v.value("full_name") == "Ada Example" and v.value("links.website") is None
    assert v.value("education.graduation") == "2027-06"


def test_every_key_offered_to_the_model_resolves(vault_dir):
    """APP-2: map_field may only name keys that code can read."""
    v = Vault.load(vault_dir)
    for key in KEYS:
        v.value(key)  # never raises


def test_missing_profile_is_a_clear_error(tmp_path):
    """VLT-1"""
    with pytest.raises(VaultError, match="profile.yaml"):
        Vault.load(tmp_path)


def test_a_clean_vault_has_no_problems(vault_dir):
    """VLT-3"""
    assert check(vault_dir) == []


@pytest.mark.parametrize("damage,problem", [
    (lambda d: (d / "cv.pdf").unlink(), "cv.pdf"),
    (lambda d: (d / "cv.pdf").write_bytes(b"not a pdf"), "not a PDF"),
    (lambda d: (d / "cv.pdf").write_bytes(PDF + b"0" * 5_300_000), "5 MB"),
    (lambda d: os.chmod(d, 0o755), "readable by others"),
    (lambda d: (d / "profile.yaml").write_text(PROFILE.replace("email: ada@example.org\n", "")), "email"),
    (lambda d: (d / "profile.yaml").write_text(PROFILE.replace("ada@example.org", "not-an-email")), "email"),
    (lambda d: (d / "profile.yaml").write_text(PROFILE + "password: hunter2\n"), "password"),
])
def test_check_reports_each_problem(vault_dir, damage, problem):
    """VLT-3 (and VLT-1: no passwords, ever)"""
    damage(vault_dir)
    assert any(problem in p for p in check(vault_dir))


@pytest.mark.parametrize("a,b,same", [
    ("Are you open to relocation for this role?", "are you open to relocation for this role", True),
    ("Are you open to relocation for this role?*", "Are you open to  relocation for this role ?", True),
    ("Do you require visa sponsorship?", "Will you now or in the future require visa sponsorship?", False),
])
def test_same_question_ignores_case_and_punctuation_only(a, b, same):
    """VLT-4"""
    assert same_question(a, b) is same


def test_saved_answers_are_found_and_survive_a_reload(vault_dir):
    """VLT-4"""
    v = Vault.load(vault_dir)
    assert v.answer_for("Are you open to relocation?") is None
    v.save_answer("Are you open to relocation?", "Yes")
    v.save_answer("Do you require visa sponsorship?", "Yes")
    v.save_answer("Are you open to relocation?", "No")  # replaces, not duplicates
    again = Vault.load(vault_dir)
    assert again.answer_for("are you open to relocation") == "No"
    assert again.answer_for("Do you require visa sponsorship?") == "Yes"
    assert len(again.answers) == 2
    assert oct((vault_dir / "answers.yaml").stat().st_mode & 0o777) == "0o600"


def test_about_me_and_samples_are_optional(vault_dir):
    """VLT-1"""
    v = Vault.load(vault_dir)
    assert v.about_me == "" and v.samples == []
    (vault_dir / "about-me.md").write_text("I build trading bots.")
    (vault_dir / "samples").mkdir()
    (vault_dir / "samples" / "essay.md").write_text("Why I study CS.")
    v = Vault.load(vault_dir)
    assert v.about_me == "I build trading bots." and v.samples == ["Why I study CS."]


def test_guidance_comments_are_not_part_of_the_text(vault_dir):
    """VLT-1: the template's instructions never end up in an essay."""
    (vault_dir / "about-me.md").write_text("<!--\nWrite about you here.\n-->\nI build trading bots.\n")
    (vault_dir / "samples").mkdir()
    (vault_dir / "samples" / "README.md").write_text("Put samples here.")
    v = Vault.load(vault_dir)
    assert v.about_me == "I build trading bots." and v.samples == []


def test_check_names_empty_fields_in_plain_words(vault_dir):
    """VLT-3"""
    (vault_dir / "profile.yaml").write_text(PROFILE.replace("email: ada@example.org", "email:"))
    assert any(p.endswith("email is empty") for p in check(vault_dir))
