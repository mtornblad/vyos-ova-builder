.PHONY: validate show-config test dependencies build upload clean

validate:
	python3 scripts/project_config.py validate

show-config:
	python3 scripts/project_config.py show

test:
	python3 -m unittest discover -s tests -v
	python3 -m compileall -q scripts tests
	bash -n build.sh upload.sh docker/run-build.sh files/vapp-init.sh files/vyos-postconfig-bootup.script

dependencies:
	python3 scripts/download_dependencies.py

build:
	./build.sh

upload:
	./upload.sh

clean:
	@printf '%s\n' 'Generated files are under artifacts/. Remove that directory explicitly when no longer needed.'
