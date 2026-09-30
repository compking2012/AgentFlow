from agentflow.configuration import load_configuration


def test_public_research_defaults_on_and_fixed_file_can_disable(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = load_configuration(create=True)
    assert getattr(config.settings, 'research_public_web_enabled', None) is True
    path = config.config_path
    path.write_text('[app]\nresearch_public_web_enabled=false\nresearch_web_hosts=["example.com"]\n')
    disabled = load_configuration()
    assert disabled.settings.research_public_web_enabled is False
    assert disabled.settings.research_web_hosts == ['example.com']
