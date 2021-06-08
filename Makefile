test:
	black ./src
	flake8 .
	mypy .

install:
	mkdir -p $out/bin
	cp ./src/weather.py $out/bin/prometheus-exporter-weather-gov
	chmod +x $out/bin/prometheus-exporter-weather-gov