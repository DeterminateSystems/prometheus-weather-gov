import <nixpkgs/nixos/tests/make-test-python.nix> ({ pkgs, ... }:
  {
    name = "prometheus-exporter-weather-gov";
    machine = { pkgs, ... }: {
      imports = [ ./service.nix ];
    };
    testScript =
      ''
        start_all()

        machine.wait_for_unit("prometheus-exporter-weather-gov.service")
        machine.wait_for_open_port(5000)
        machine.succeed("curl http://127.0.0.1:5000/")
      '';
  })
