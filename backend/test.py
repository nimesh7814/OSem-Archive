from connection import execute_query

rows = execute_query("SELECT * FROM summary")
print(rows)