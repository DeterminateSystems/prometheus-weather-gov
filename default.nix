let
  pkgs = import <nixpkgs> { };
  myPython = pkgs.python3.withPackages
    (pypkgs: [
      pypkgs.requests
      pypkgs.flask
      pypkgs.prometheus_client
      pypkgs.pendulum
    ]);
in
pkgs.stdenv.mkDerivation
{
  name = "prometheus-weather-gov";
  src = ./.;
  buildInputs = [
    myPython
    pkgs.python3.pkgs.mypy
    pkgs.python3.pkgs.flake8
    pkgs.python3.pkgs.black
  ];
}
