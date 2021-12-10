# service.nix
let
  exporter = import ./default.nix;
in {
  networking.firewall.allowedTCPPorts = [ 5000 ];

  systemd.services.prometheus-exporter-weather-gov = {
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      DynamicUser = true;
      ExecStart = "${exporter}/bin/prometheus-exporter-weather-gov";
    };
  };
}
