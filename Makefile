.PHONY: build up down logs

build:
	docker compose --profile build build runner-image

up: build
	docker compose up -d --build coordinator

# stop coordinator AND deregister/remove all runners
down:
	docker compose run --rm coordinator --down
	docker compose down

logs:
	docker compose logs -f coordinator
