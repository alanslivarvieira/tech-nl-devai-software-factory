from greeter import greet


def test_default_greeting():
    assert greet() == "Hello, world!"
