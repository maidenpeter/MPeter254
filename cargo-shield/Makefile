PYTHON ?= python3
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: install seed train run demo reset test report doctor

install:  ## one-time: install Python packages (needs internet)
	$(PYTHON) -m pip install -r requirements.txt

seed:     ## generate data, train the model, score everything, save the reset snapshot
	$(PYTHON) -m cargoshield.seed --bookings 3000

train:    ## retrain the anomaly model and rescore (after editing config/rules.yaml)
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
