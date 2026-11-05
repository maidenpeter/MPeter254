ifeq ($(OS),Windows_NT)
PYTHON ?= python
else
PYTHON ?= python3
endif
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: install seed train run demo reset test report doctor

install:  ## one-time: install Python packages (needs internet)
	$(PYTHON) -m pip install -r requirements.txt

seed:     ## generate data, train the model, score everything, save the reset snapshot
	$(PYTHON) -m cargoshield.seed --bookings 3000

train:    ## after editing config/rules.yaml: reset to the demo start, retrain, rescore, save as the new start
	$(PYTHON) -m cargoshield train

run:      ## start the app on http://localhost:8000
	$(PYTHON) -m cargoshield run --host $(HOST) --port $(PORT)

demo:     ## seed + train + start, in one go
	$(PYTHON) -m cargoshield demo --host $(HOST) --port $(PORT)

reset:    ## put the database back to the demo starting state
	$(PYTHON) -m cargoshield reset

test:     ## run the test suite
	$(PYTHON) -m pytest

report:   ## planted risks caught and false-positive rate
	$(PYTHON) -m cargoshield report

doctor:   ## pre-flight check before presenting
	$(PYTHON) -m cargoshield doctor
