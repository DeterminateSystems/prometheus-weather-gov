let
  pkgs = import <nixpkgs> { };
  exporter = import ./default.nix;
in
pkgs.dockerTools.buildLayeredImage {
  name = "prometheus-exporter-weather-gov";
  config = {
    Entrypoint = "${exporter}/bin/prometheus-exporter-weather-gov";
    ExposedPorts."5000/tcp" = { };
  };
}
