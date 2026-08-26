package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"time"
)

const timeout = 2 * time.Second

func requireTCP(address string) error {
	connection, err := net.DialTimeout("tcp", address, timeout)
	if err != nil {
		return fmt.Errorf("%s is unavailable: %w", address, err)
	}
	return connection.Close()
}

func requireMaster() error {
	client := http.Client{Timeout: timeout}
	response, err := client.Get("http://127.0.0.1:9333/dir/status")
	if err != nil {
		return fmt.Errorf("master status is unavailable: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("master status returned HTTP %d", response.StatusCode)
	}
	return nil
}

func main() {
	for _, address := range []string{"127.0.0.1:9333", "127.0.0.1:8333"} {
		if err := requireTCP(address); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
	if err := requireMaster(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
