# Thin aliases. `./test.sh` is the one command and works without make.
.PHONY: test db db-down

test:
	./test.sh

db:
	docker compose up -d --wait

db-down:
	docker compose down
